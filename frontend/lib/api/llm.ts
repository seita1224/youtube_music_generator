// backend `/llm` の型付きクライアント(contracts/backend-api.yaml、 ADR-0019 / ADR-0024)。
//
// GET /llm/providers は { active, providers } を返す。 PUT は provider/auth_mode/model を
// app_state に永続化する。 credential は write-only (平文・マスクは応答に出ない)。
// Codex OAuth は未配線のため PUT が 422。 env が SoT のとき credential 操作は 409。

import { apiDelete, apiGet, apiPut } from "@/lib/api/client";

export type LlmProvider = "openai" | "anthropic" | "ollama";
export type LlmAuthMode = "api_key" | "codex_oauth";
export type LlmCredentialSource = "env" | "db" | "none" | "n/a";
export type LlmSecretProvider = "openai" | "anthropic";

/** API key 最大長 (backend / OpenAPI と同一。入力 DoS 防止)。 */
export const LLM_API_KEY_MAX_LENGTH = 2048;

/** 各 provider の設定可否 + 選択肢 + credential メタデータ。 */
export interface LlmProviderConfig {
  readonly provider: LlmProvider;
  readonly available: boolean;
  readonly auth_modes: readonly LlmAuthMode[];
  readonly models: readonly string[];
  readonly credential_source: LlmCredentialSource;
  readonly credential_configured: boolean;
  readonly unsupported_auth_modes?: readonly LlmAuthMode[];
}

/** 現在 active な provider 設定。 */
export interface LlmActiveState {
  readonly provider: LlmProvider;
  readonly auth_mode: LlmAuthMode;
  readonly model: string;
}

/** GET /llm/providers のレスポンス。 */
export interface LlmSettings {
  readonly active: LlmActiveState;
  readonly providers: readonly LlmProviderConfig[];
}

/** provider 別の当月コスト/トークン内訳。 */
export interface LlmProviderByUsage {
  readonly cost_usd: number;
  readonly prompt_tokens: number;
  readonly cached_tokens: number;
  readonly completion_tokens: number;
}

/** 月次 LLM 使用量 + 予算進捗。 */
export interface LlmUsage {
  readonly month: string;
  readonly total_cost_usd: number;
  readonly budget_usd: number;
  readonly budget_pct: number;
  readonly by_provider: Readonly<Record<string, LlmProviderByUsage>>;
}

/** PUT /llm/providers の body。 */
export interface SetProviderBody {
  readonly provider: LlmProvider;
  readonly auth_mode: LlmAuthMode;
  readonly model: string;
}

/** PUT /llm/providers のレスポンス。 */
export interface LlmProviderState {
  readonly provider: LlmProvider;
  readonly auth_mode: LlmAuthMode;
  readonly model: string;
}

/** credential 書込/削除後のメタデータのみ。 */
export interface LlmCredentialState {
  readonly provider: LlmSecretProvider;
  readonly credential_source: LlmCredentialSource;
  readonly credential_configured: boolean;
}

/** LLM 設定(active + providers)を取得する。 */
export async function getLlmSettings(): Promise<LlmSettings> {
  return apiGet<LlmSettings>("/llm/providers");
}

/** @deprecated getLlmSettings を使う。 互換のため providers 配列のみ返す。 */
export async function getProviders(): Promise<LlmProviderConfig[]> {
  const settings = await getLlmSettings();
  return [...settings.providers];
}

/**
 * active な LLM provider / model を切り替える。
 *
 * codex_oauth は 422、 不正 model は 422、 anthropic + 非 api_key は 400。
 */
export async function setProvider(
  provider: LlmProvider,
  authMode: LlmAuthMode,
  model: string,
): Promise<LlmProviderState> {
  const body: SetProviderBody = {
    provider,
    auth_mode: authMode,
    model,
  };
  return apiPut<LlmProviderState>("/llm/providers", body);
}

/** API key を write-only で設定/置換する。 env SoT 時は 409。 前後空白は trim。 */
export async function setCredential(
  provider: LlmSecretProvider,
  apiKey: string,
): Promise<LlmCredentialState> {
  return apiPut<LlmCredentialState>("/llm/credentials", {
    provider,
    api_key: apiKey.trim(),
  });
}

/** DB 保存の API key のみ削除する。 env SoT 時は 409。 */
export async function clearCredential(
  provider: LlmSecretProvider,
): Promise<LlmCredentialState> {
  return apiDelete<LlmCredentialState>(`/llm/credentials/${provider}`);
}

/** 月次 LLM 使用量を取得する。 */
export async function getUsage(month?: string): Promise<LlmUsage> {
  return apiGet<LlmUsage>(month ? `/llm/usage?month=${month}` : "/llm/usage");
}
