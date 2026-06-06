// T068: backend `/health` の型付きクライアント(contracts/backend-api.yaml の HealthResponse)。
//
// `/health` は非認証エンドポイント(screen-spec.md §4)だが、 authFetch 経由でも
// Authorization は無害に無視される。 client.apiGet を通すことでパス正規化を共通化する。
//
// 型は contract と整合させたローカル定義。 schema.ts 生成後は
// `paths["/health"]["get"]["responses"]["200"]["content"]["application/json"]` に
// 差し替え可能(構造は一致させてある)。

import { apiGet } from "@/lib/api/client";

export type ServiceStatus = "ok" | "degraded";
export type GpuWorkerStatus = "ok" | "unreachable";

export interface HealthResponse {
  readonly status: ServiceStatus;
  readonly db?: ServiceStatus;
  readonly gpu_worker?: GpuWorkerStatus;
  readonly scheduler_enabled?: boolean;
  readonly dryrun_enabled?: boolean;
  readonly llm_provider?: string;
}

/** backend liveness + 依存状態を取得する。 */
export async function getHealth(): Promise<HealthResponse> {
  return apiGet<HealthResponse>("/health");
}
