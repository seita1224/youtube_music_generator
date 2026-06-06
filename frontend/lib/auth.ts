// T065: Basic 認証ヘッダ注入の fetch wrapper (ADR-0013)。
//
// backend は LAN 内 Basic 認証。 next.config.ts の rewrites で
// `/api/backend/:path*` → backend にプロキシされる(ADR-0013 / ADR-0029)。
// このラッパは全 API 呼び出しの単一窓口とし、 Authorization ヘッダを注入する。
//
// 資格情報の出所(優先順):
//  1. ブラウザの Basic 認証セッション(ヘッダ省略でブラウザが付与する)
//  2. ビルド時 env `NEXT_PUBLIC_BASIC_AUTH_USER` / `..._PASSWORD`(任意)
// 資格情報はソースにハードコードしない(env のみ、 .gitignore 管理)。

const API_PREFIX = "/api/backend";

interface BasicCredentials {
  readonly user: string;
  readonly password: string;
}

/**
 * env から Basic 認証クレデンシャルを読む。 未設定なら null。
 * 未設定時はブラウザ標準の Basic 認証セッションに委ねる(ADR-0013)。
 */
function readCredentials(): BasicCredentials | null {
  const user = process.env.NEXT_PUBLIC_BASIC_AUTH_USER;
  const password = process.env.NEXT_PUBLIC_BASIC_AUTH_PASSWORD;
  if (!user || !password) {
    return null;
  }
  return { user, password };
}

/** UTF-8 安全な base64 エンコード(btoa は Latin-1 のみのため TextEncoder 経由)。 */
function toBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

/** Authorization: Basic ヘッダ値を返す。 クレデンシャル未設定なら null。 */
export function basicAuthHeader(): string | null {
  const credentials = readCredentials();
  if (!credentials) {
    return null;
  }
  return `Basic ${toBase64(`${credentials.user}:${credentials.password}`)}`;
}

/** `/health` のような絶対/相対パスを backend プロキシ配下の URL に正規化する。 */
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
 * Basic 認証ヘッダを注入する fetch wrapper。
 *
 * @param path backend 上のパス(例: `/health`)。 `/api/backend` 前置は自動付与。
 * @param init  追加の RequestInit(headers はマージ、 Authorization は上書きされない)。
 */
export async function authFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const auth = basicAuthHeader();
  if (auth && !headers.has("Authorization")) {
    headers.set("Authorization", auth);
  }
  return fetch(resolvePath(path), { ...init, headers });
}
