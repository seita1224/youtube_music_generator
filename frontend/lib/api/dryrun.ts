// backend `/dryrun` の型付きクライアント(contracts/backend-api.yaml の DryrunOutput)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(health.ts / scheduler.ts と同方針)。
// 一覧/承認/却下のみを扱う。 動画は API クライアント経由ではなく
// `<video src="/api/backend/dryrun/outputs/{id}/video" />` で同一オリジン proxied
// パス再生する(ブラウザの Basic 認証セッションを利用、 authFetch は通らない)。

import { apiGet, apiPost } from "@/lib/api/client";

export type DryrunState =
  | "pending"
  | "approved"
  | "rejected"
  | "auto_expired"
  | "posted";

export interface DryrunOutput {
  readonly id: string; // uuid
  readonly post_id: string; // uuid
  readonly state: DryrunState;
  readonly video_uri: string;
  readonly reject_reason?: string;
  readonly created_at: string; // date-time
  readonly reviewed_at?: string; // date-time
  // posted_at / auto_expired_at は contracts schema 未掲載のため任意拡張に留める。
  readonly posted_at?: string; // date-time
  readonly auto_expired_at?: string; // date-time
}

export interface DryrunListResponse {
  readonly items: readonly DryrunOutput[];
}

/** dryrun 出力一覧を取得する。 state 指定でフィルタ可能。 */
export async function listDryrunOutputs(
  state?: DryrunState,
): Promise<DryrunListResponse> {
  const q = state ? `?state=${state}` : "";
  return apiGet<DryrunListResponse>(`/dryrun/outputs${q}`);
}

/** pending の dryrun 出力を承認し、 投稿する(state→posted)。 */
export async function approveDryrun(id: string): Promise<DryrunOutput> {
  return apiPost<DryrunOutput>(`/dryrun/outputs/${id}/approve`);
}

/** pending の dryrun 出力を却下する(reason は min 4 文字、 呼び出し側で検証)。 */
export async function rejectDryrun(id: string, reason: string): Promise<void> {
  return apiPost<void>(`/dryrun/outputs/${id}/reject`, { reason });
}
