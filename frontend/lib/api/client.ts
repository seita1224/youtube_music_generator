// T066: backend API の薄いクライアント雛形。
//
// 型は openapi-typescript で生成した `lib/api/schema.ts` の `paths` を使う想定だが、
// schema.ts は生成物(`npm run gen:api`)であり未生成でも本ファイルは型エラーにならない。
// そのため schema.ts への直接 import はせず、 呼び出し側が response 型を型引数で渡す。
// 生成後は各ラッパ(例: lib/api/health.ts)が `paths["/health"]...` を渡す。

import { authFetch } from "@/lib/auth";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** 共通レスポンス処理。 非 2xx は ApiError、 ボディは JSON として型 T で返す。 */
async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new ApiError(
      response.status,
      detail || `${response.status} ${response.statusText}`,
    );
  }
  // 204 No Content / 空ボディ(apiPost<void>・apiPut<void> の reject 等)は undefined を返す。
  // response.json() は空ボディで例外を投げるため、 一旦 text を読んで判定する。
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  return (text === "" ? undefined : JSON.parse(text)) as T;
}

/** GET。 response ボディを型 T として返す。 */
export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authFetch(path, { ...init, method: "GET" });
  return parse<T>(response);
}

/** JSON body 付き PUT。 */
export async function apiPut<T>(
  path: string,
  body: unknown,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  const response = await authFetch(path, {
    ...init,
    method: "PUT",
    headers,
    body: JSON.stringify(body),
  });
  return parse<T>(response);
}

/** JSON body 付き POST。 */
export async function apiPost<T>(
  path: string,
  body?: unknown,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const response = await authFetch(path, {
    ...init,
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse<T>(response);
}

/** DELETE。 body なし。 */
export async function apiDelete<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await authFetch(path, { ...init, method: "DELETE" });
  return parse<T>(response);
}
