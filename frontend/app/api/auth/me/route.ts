import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import {
  SESSION_COOKIE_NAME,
  verifySessionToken,
} from "@/lib/server/session";

export const runtime = "nodejs";

/** 現在のセッションユーザー名を返す(未認証は 401)。 */
export async function GET(): Promise<Response> {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE_NAME)?.value;
  const session = await verifySessionToken(token);
  if (!session) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  return NextResponse.json({ username: session.username });
}
