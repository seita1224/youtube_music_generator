// backend `/analytics` の型付きクライアント(contracts/backend-api.yaml の AnalyticsOverview)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(dryrun.ts / health.ts と同方針)。
// /analytics/summary が LLM 入力寄りの生集計を返すのに対し、 本 endpoint は
// frontend のチャート + 推奨テーブルが直接消費できる形(by_genre 配列 +
// recommendations 配列)を返す(US3 T110)。

import { apiGet } from "@/lib/api/client";

/** ジャンル別 retention/views 集計。 avg_retention_pct は標本ゼロで null。 */
export interface GenreAnalytics {
  readonly genre: string;
  readonly role: string;
  readonly video_count: number;
  readonly avg_retention_pct: number | null;
  readonly total_views: number;
}

/** 推奨アクション。 adopt=主力昇格 / drop=削除 / keep=継続観察。 */
export type RecommendAction = "adopt" | "drop" | "keep";

/** experiment ジャンルの採用/削除推奨判定(主力平均 retention 比ベース)。 */
export interface GenreRecommendation {
  readonly genre: string;
  readonly action: RecommendAction;
  // 主力ジャンル平均 retention に対する比(>=0.8 で adopt / <0.6 で drop)。
  // 主力標本不足・experiment 標本不足では null。
  readonly retention_ratio_to_primary: number | null;
  readonly sample_size: number; // experiment で投下した本数(>=4 が判定の前提)
  readonly days_elapsed: number; // 実験開始からの経過日数(>=14 が判定の前提)
  readonly rationale: string;
}

/** `/analytics` の応答全体。 by_genre=チャート、 recommendations=推奨テーブル。 */
export interface AnalyticsOverview {
  readonly window_days: number;
  readonly sample_size: number;
  readonly by_genre: readonly GenreAnalytics[];
  readonly recommendations: readonly GenreRecommendation[];
}

/**
 * ジャンル別 retention/views + 採用/削除推奨を取得する。
 *
 * @param windowDays 集計ウィンドウ日数(未指定なら backend 既定 14)。
 */
export async function getAnalytics(
  windowDays?: number,
): Promise<AnalyticsOverview> {
  const q =
    windowDays === undefined ? "" : `?window_days=${windowDays}`;
  return apiGet<AnalyticsOverview>(`/analytics${q}`);
}
