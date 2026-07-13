import { NextResponse } from "next/server";

import { verifyAdminCredentials } from "@/lib/server/credentials";
import { assertCsrfForUnsafe } from "@/lib/server/csrf";
import {
  GLOBAL_LOGIN_BUCKET_KEY,
  clearLoginAttempts,
  isLoginRateLimited,
  recordLoginFailure,
} from "@/lib/server/rate-limit";
import {
  SESSION_COOKIE_NAME,
  createSessionToken,
  sessionCookieOptions,
} from "@/lib/server/session";

export const runtime = "nodejs";

const GENERIC_ERROR = "ユーザー名またはパスワードが正しくありません";

interface LoginBody {
  username?: unknown;
  password?: unknown;
}

export async function POST(request: Request): Promise<Response> {
  const csrf = assertCsrfForUnsafe(request);
  if (csrf) {
    return csrf;
  }

  // クライアント捏造可能な X-Forwarded-For 等は使わずグローバルバケット。
  const key = GLOBAL_LOGIN_BUCKET_KEY;
  if (isLoginRateLimited(key)) {
    return NextResponse.json(
      { detail: "試行回数が上限に達しました。しばらくしてから再試行してください" },
      { status: 429 },
    );
  }

  let body: LoginBody;
  try {
    body = (await request.json()) as LoginBody;
  } catch {
    recordLoginFailure(key);
    return NextResponse.json({ detail: GENERIC_ERROR }, { status: 401 });
  }

  const username =
    typeof body.username === "string" ? body.username.trim() : "";
  const password = typeof body.password === "string" ? body.password : "";

  if (!username || !password) {
    recordLoginFailure(key);
    return NextResponse.json({ detail: GENERIC_ERROR }, { status: 401 });
  }

  const ok = verifyAdminCredentials(username, password);
  if (!ok) {
    recordLoginFailure(key);
    // クレデンシャルはログに出さない
    return NextResponse.json({ detail: GENERIC_ERROR }, { status: 401 });
  }

  clearLoginAttempts(key);

  let token: string;
  try {
    token = await createSessionToken(username);
  } catch {
    return NextResponse.json(
      { detail: "認証の設定が不完全です" },
      { status: 500 },
    );
  }

  const response = NextResponse.json({ ok: true, username });
  response.cookies.set(SESSION_COOKIE_NAME, token, sessionCookieOptions());
  return response;
}
