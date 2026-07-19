// backend `/plans` の型付きクライアント(contracts/backend-api.yaml の Plan)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(health.ts / scheduler.ts / dryrun.ts と同方針)。

import { apiGet, apiPost } from "@/lib/api/client";

export type PlanCycle = "daily" | "weekly";

export type PlanStatus =
  | "generated"
  | "approved"
  | "executing"
  | "music_generated"
  | "completed"
  | "failed";

export interface Plan {
  readonly id: string; // uuid
  readonly cycle: PlanCycle;
  // daily は target_date、 weekly は target_week_start のどちらかが入る(相互排他)。
  readonly target_date?: string; // date
  readonly target_week_start?: string; // date
  // DailyPlan / WeeklyPlan の Pydantic dump。 中身は cycle により構造が異なる。
  readonly payload: Record<string, unknown>;
  readonly rationale: string;
  readonly status: PlanStatus;
  readonly llm_provider: string;
  readonly llm_model: string;
  readonly llm_prompt_version: string;
  readonly llm_cost_usd: number;
  readonly created_at: string; // date-time
  readonly approved_at?: string; // date-time
}

export interface PlanListResponse {
  readonly items: readonly Plan[];
  readonly total: number;
}

/** POST /plans の body。 weekly は backend 未対応のため UI は daily のみ送る。 */
export interface GeneratePlanRequest {
  readonly cycle: PlanCycle;
  readonly target_date: string; // YYYY-MM-DD
  readonly force_regenerate?: boolean;
}

/** プラン一覧を取得する。 cycle(daily/weekly)/ status でフィルタ可能。 */
export async function listPlans(
  cycle?: PlanCycle,
  status?: PlanStatus,
): Promise<PlanListResponse> {
  const params = new URLSearchParams();
  if (cycle) {
    params.set("cycle", cycle);
  }
  if (status) {
    params.set("status", status);
  }
  const query = params.toString();
  return apiGet<PlanListResponse>(`/plans${query ? `?${query}` : ""}`);
}

/** improvement-plan LLM で新規 Plan を生成する(POST /plans → 201)。 */
export async function generatePlan(body: GeneratePlanRequest): Promise<Plan> {
  return apiPost<Plan>("/plans", body);
}

/** 単一 Plan を取得する(詳細画面用)。 */
export async function getPlan(id: string): Promise<Plan> {
  return apiGet<Plan>(`/plans/${id}`);
}

/** generated のプランを承認する(status→approved、 音楽生成実行を解錠)。 */
export async function approvePlan(id: string): Promise<Plan> {
  return apiPost<Plan>(`/plans/${id}/approve`);
}
