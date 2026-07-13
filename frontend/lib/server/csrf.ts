// same-origin チェック(login / logout / BFF の unsafe メソッド用 CSRF 緩和)。
// SameSite=Lax cookie に加え、 Origin / Referer の Host 一致を要求する。
// LAN 利用時はアクセスした Host(例: 192.168.x.x:3000)と Origin が一致すれば通る。

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function isUnsafeMethod(method: string): boolean {
  return UNSAFE_METHODS.has(method.toUpperCase());
}

/** リクエスト Host と Origin/Referer の host が一致するか。 */
export function isSameOriginRequest(request: Request): boolean {
  const host = request.headers.get("host");
  if (!host) {
    return false;
  }

  const origin = request.headers.get("origin");
  if (origin) {
    try {
      return new URL(origin).host === host;
    } catch {
      return false;
    }
  }

  const referer = request.headers.get("referer");
  if (referer) {
    try {
      return new URL(referer).host === host;
    } catch {
      return false;
    }
  }

  // unsafe メソッドで Origin/Referer 欠落は拒否(クロスサイト古典的 CSRF 緩和)。
  return false;
}

export function assertCsrfForUnsafe(request: Request): Response | null {
  if (!isUnsafeMethod(request.method)) {
    return null;
  }
  if (isSameOriginRequest(request)) {
    return null;
  }
  return Response.json({ detail: "Forbidden" }, { status: 403 });
}
