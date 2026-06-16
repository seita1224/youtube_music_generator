// backend `/prompts` の型付きクライアント(contracts/backend-api.yaml の
// PromptSummary / PromptVersion、 FR-036 / T135)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(dryrun.ts / llm.ts と同方針)。
// MVP 範囲は read + preview + version 切替のみ(ファイル書込/編集は範囲外)。
//   - GET /prompts?area=        : area/name ごとの利用可能 version 一覧
//   - GET /prompts/{prompt_name}: 指定 version(省略=最新)の本文プレビュー
//     prompt_name は "<area>/<name>"(例 "planner/system")で、 version 接尾辞は付けない。

import { apiGet } from "@/lib/api/client";

/** area/name ごとの利用可能 version 一覧(GET /prompts の 1 要素)。 */
export interface PromptSummary {
  readonly area: string;
  readonly name: string;
  // 昇順の version 番号一覧(例 [1, 2])。
  readonly versions: readonly number[];
  // 最新 version 番号(versions の末尾相当、 contract optional)。
  readonly latest?: number;
}

/** 指定 version の本文プレビュー(GET /prompts/{prompt_name})。 */
export interface PromptVersion {
  // ResolvedPrompt.ref(例 "planner/system_v1")。
  readonly name: string;
  // version 番号の文字列表現(例 "1")。
  readonly version: string;
  // プロンプト本文(ResolvedPrompt.text)。
  readonly content: string;
  readonly updated_at?: string; // date-time
}

/** GET /prompts のレスポンス(ラッパ付き、 dryrun と同形式)。 */
export interface PromptListResponse {
  readonly items: readonly PromptSummary[];
}

/** プロンプト一覧を取得する。 area 指定で絞り込み可能。 */
export async function listPrompts(area?: string): Promise<PromptListResponse> {
  return apiGet<PromptListResponse>(area ? `/prompts?area=${area}` : "/prompts");
}

/**
 * 指定プロンプトの本文を取得する(read-only プレビュー)。
 *
 * @param promptName "<area>/<name>" 形式(例 "planner/system")。 version 接尾辞は付けない。
 * @param version    取得する version 番号。 省略時は最新版を解決する。
 */
export async function getPrompt(
  promptName: string,
  version?: number,
): Promise<PromptVersion> {
  const q = version != null ? `?version=${version}` : "";
  return apiGet<PromptVersion>(`/prompts/${promptName}${q}`);
}
