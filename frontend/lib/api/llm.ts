// backend `/llm` の型付きクライアント(contracts/backend-api.yaml の
// LlmProviderConfig / LlmUsage、 ADR-0019 / ADR-0024)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(dryrun.ts / scheduler.ts / analytics.ts と同方針)。
// GET /llm/providers は配列直返し(ラッパ無し)。 PUT /llm/providers は
// {provider, auth_mode} を受け、 不正組合せ(anthropic + codex_oauth など)は
// ApiError(status=400) を throw する。 GET /llm/usage は月次コスト集計。
//
// 注意: active provider の model は app_state に保存されず provider 既定固定のため、
// PUT body に model は含めない(contract `required: [provider, auth_mode]`)。 model は
// 画面では参考表示(read-only)に留める。

import { apiGet, apiPut } from "@/lib/api/client";

export type LlmProvider = "openai" | "anthropic" | "ollama";
export type LlmAuthMode = "api_key" | "codex_oauth";

/** 各 provider の設定可否 + 選択肢(GET /llm/providers の 1 要素)。 */
export interface LlmProviderConfig {
  readonly provider: LlmProvider;
  // 対応 API key/接続が設定済みか(openai/anthropic は key 非空、 ollama は常に true)。
  readonly available: boolean;
  readonly auth_modes: readonly LlmAuthMode[];
  // 選択 provider の対応モデル(contract optional)。 app_state 非保存=参考表示用。
  readonly models?: readonly string[];
}

/** provider 別の当月コスト/トークン内訳(LlmUsage.by_provider の値)。 */
export interface LlmProviderByUsage {
  readonly cost_usd: number;
  readonly prompt_tokens: number;
  readonly cached_tokens: number;
  readonly completion_tokens: number;
}

/** 月次 LLM 使用量 + 予算進捗(GET /llm/usage)。 */
export interface LlmUsage {
  readonly month: string; // YYYY-MM
  readonly total_cost_usd: number;
  readonly budget_usd: number;
  readonly budget_pct: number; // total / budget * 100(budget=0 は 0)
  readonly by_provider: Readonly<Record<string, LlmProviderByUsage>>;
}

/** PUT /llm/providers の body。 contract `required: [provider, auth_mode]`。 */
export interface SetProviderBody {
  readonly provider: LlmProvider;
  readonly auth_mode: LlmAuthMode;
}

/** provider 設定の選択肢一覧を取得する(配列直返し、 ラッパ無し)。 */
export async function getProviders(): Promise<LlmProviderConfig[]> {
  return apiGet<LlmProviderConfig[]>("/llm/providers");
}

/**
 * active な LLM provider を切り替える(ADR-0019)。
 *
 * 不正組合せ(例: anthropic + codex_oauth、 FR-022)は backend が 400 を返し、
 * client は ApiError(status=400) を throw する → UI で組合せエラー表示。
 */
export async function setProvider(
  provider: LlmProvider,
  authMode: LlmAuthMode,
): Promise<void> {
  const body: SetProviderBody = { provider, auth_mode: authMode };
  return apiPut<void>("/llm/providers", body);
}

/**
 * 月次 LLM 使用量を取得する(ADR-0024)。
 *
 * @param month 集計対象月 `YYYY-MM`(未指定なら backend 既定=当月 UTC)。
 */
export async function getUsage(month?: string): Promise<LlmUsage> {
  return apiGet<LlmUsage>(month ? `/llm/usage?month=${month}` : "/llm/usage");
}
