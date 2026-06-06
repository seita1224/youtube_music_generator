// backend `/scheduler` の型付きクライアント(contracts/backend-api.yaml の SchedulerState)。

import { apiGet } from "@/lib/api/client";

export interface SchedulerState {
  readonly enabled: boolean;
  readonly updated_at?: string;
}

/** scheduler の有効/無効状態を取得する。 */
export async function getSchedulerState(): Promise<SchedulerState> {
  return apiGet<SchedulerState>("/scheduler");
}
