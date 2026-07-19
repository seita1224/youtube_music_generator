// T065: same-origin cookie セッション向け fetch wrapper (ADR-0013)。
//
// `/api/backend/:path*` は Next Route Handler(BFF)がセッション検証し、
// サーバ側で backend Basic Authorization を注入する。
// クライアントに ADMIN_* / Basic 資格情報を埋め込まない。

const API_PREFIX = "/api/backend";

/** `/health` のような絶対/相対パスを backend BFF 配下の URL に正規化する。 */
function resolvePath(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  if (path.startsWith(API_PREFIX)) {
    return path;
  }
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_PREFIX}${normalized}`;
}

/**
 * セッション cookie 付きで backend BFF を叩く fetch wrapper。
 *
 * @param path backend 上のパス(例: `/health`)。 `/api/backend` 前置は自動付与。
 * @param init  追加の RequestInit(credentials は same-origin 固定)。
 */
export async function authFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  return fetch(resolvePath(path), {
    ...init,
    headers,
    credentials: "same-origin",
  });
}
