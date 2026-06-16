// backend `/mvp-check` の型付きクライアント(contracts/backend-api.yaml の
// MvpCheck / MvpCheckItem、 ADR-0035 §6 の MVP 6 項目判定)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(llm.ts / dryrun.ts と同方針)。 GET /mvp-check は
// read-only で 6 項目の green/red と completed(0..6) を返す。

import { apiGet } from "@/lib/api/client";

/** MVP 判定 6 項目の固定 id(順序も契約で固定)。 */
export type MvpCheckId =
  | "dryrun_success"
  | "acoustid_clear"
  | "unlisted_post"
  | "panic_stop"
  | "oauth_refresh"
  | "slack_categories";

/** 1 項目の判定結果(GET /mvp-check の items 要素)。 */
export interface MvpCheckItem {
  readonly id: MvpCheckId;
  readonly label: string;
  readonly status: "green" | "red";
  readonly detail?: string;
}

/** MVP チェックリスト全体(GET /mvp-check)。 total は固定 6。 */
export interface MvpCheck {
  readonly items: readonly MvpCheckItem[];
  readonly completed: number; // 0..6
  readonly total: number; // = 6
}

/** MVP 6 項目の判定結果を取得する(read-only、 ADR-0035 §6)。 */
export async function getMvpCheck(): Promise<MvpCheck> {
  return apiGet<MvpCheck>("/mvp-check");
}
