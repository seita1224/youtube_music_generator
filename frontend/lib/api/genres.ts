// backend `/genres` の型付きクライアント(contracts/backend-api.yaml の Genre 群、 FR-037/038)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に手書きで一致させる
// (scheduler.ts / llm.ts と同方針)。 GET /genres は {items,total} 封筒。 promote/disable は
// GenreResponse を返し、 不在は 404・不正遷移(primary を更に昇格等)は 409 で ApiError になる。

import { apiGet, apiPost } from "@/lib/api/client";

// Genre.role は enum ではなく文字列運用(primary / extended / experimental)。
export type GenreRole = "primary" | "extended" | "experimental";

/** ジャンル辞書の 1 レコード(GET /genres の items 要素、 promote/disable の応答)。 */
export interface Genre {
  readonly name: string; // 主キー(英ジャンル slug)
  readonly display_name: string;
  readonly bpm_min?: number | null;
  readonly bpm_max?: number | null;
  readonly description?: string | null;
  // 運用上 primary/extended/experimental 以外の値も来うるため string で受ける。
  readonly role: string;
  readonly enabled: boolean;
  readonly created_at: string; // date-time
  readonly updated_at: string; // date-time
}

/** GET /genres のレスポンス封筒。 */
export interface GenreListResponse {
  readonly items: readonly Genre[];
  readonly total: number;
}

/** ジャンル一覧を取得する。 enabled / role でフィルタ可能。 */
export async function listGenres(filters?: {
  readonly enabled?: boolean;
  readonly role?: string;
}): Promise<GenreListResponse> {
  const params = new URLSearchParams();
  if (filters?.enabled !== undefined) {
    params.set("enabled", String(filters.enabled));
  }
  if (filters?.role) {
    params.set("role", filters.role);
  }
  const q = params.toString();
  return apiGet<GenreListResponse>(`/genres${q ? `?${q}` : ""}`);
}

/**
 * ジャンルを採用方向へ昇格する(FR-038)。 enabled=true + role を 1 段昇格
 * (experimental → extended → primary)。 target_role 明示も可。
 * primary を更に昇格する等の不正遷移は backend が 409 を返し ApiError になる。
 */
export async function promoteGenre(
  name: string,
  options?: { readonly targetRole?: GenreRole; readonly reason?: string },
): Promise<Genre> {
  return apiPost<Genre>(`/genres/${encodeURIComponent(name)}/promote`, {
    target_role: options?.targetRole,
    reason: options?.reason,
  });
}

/** ジャンルを無効化する(FR-038)。 enabled=false。 次回サイクル以降 daily_cycle が拾わない。 */
export async function disableGenre(
  name: string,
  reason?: string,
): Promise<Genre> {
  return apiPost<Genre>(`/genres/${encodeURIComponent(name)}/disable`, {
    reason,
  });
}
