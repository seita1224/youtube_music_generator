import { NextResponse } from "next/server";

import { assertCsrfForUnsafe } from "@/lib/server/csrf";
import {
  SESSION_COOKIE_NAME,
  sessionCookieOptions,
} from "@/lib/server/session";

export const runtime = "nodejs";

function clearSessionCookie(response: NextResponse): void {
  const opts = sessionCookieOptions();
  // 発行時と同じ path / secure / sameSite で消す(不一致だと残る)。
  response.cookies.set(SESSION_COOKIE_NAME, "", {
    httpOnly: opts.httpOnly,
    secure: opts.secure,
    sameSite: opts.sameSite,
    path: opts.path,
    maxAge: 0,
  });
}

/** セッション cookie を破棄する。 CSRF 付き POST のみ許可。 */
export async function POST(request: Request): Promise<Response> {
  const csrf = assertCsrfForUnsafe(request);
  if (csrf) {
    return csrf;
  }
  const response = NextResponse.json({ ok: true });
  clearSessionCookie(response);
  return response;
}
