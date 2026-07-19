import { NextResponse, type NextRequest } from "next/server";

import { safeNextPath } from "@/lib/safe-next-path";
import {
  SESSION_COOKIE_NAME,
  verifySessionToken,
} from "@/lib/server/session";

// 管理画面と BFF をセッションで保護する。
// 公開: /login, /api/auth/*, 静的アセット。

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  const isLoginPage = pathname === "/login";
  const isAuthApi = pathname.startsWith("/api/auth/");
  const isBackendApi = pathname.startsWith("/api/backend/");

  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  const session = await verifySessionToken(token);

  if (isAuthApi) {
    return NextResponse.next();
  }

  if (isLoginPage) {
    if (session) {
      const dest = safeNextPath(
        request.nextUrl.searchParams.get("next"),
        request.url,
      );
      return NextResponse.redirect(new URL(dest, request.url));
    }
    return NextResponse.next();
  }

  if (!session) {
    if (isBackendApi || pathname.startsWith("/api/")) {
      return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
    }
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", `${pathname}${request.nextUrl.search}`);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * 静的ファイルと Next 内部を除外。
     * - _next/static, _next/image, favicon, 画像等
     */
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
