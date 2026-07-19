// backend `/jobs` の型付きクライアント(contracts/backend-api.yaml の JobRun / JobStepEvent)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(health.ts / scheduler.ts と同方針)。
// 初期表示は DB snapshot(listJobRuns / listJobRunEvents)、 以後 SSE は sse.ts。

import { apiGet } from "@/lib/api/client";

export type JobRunStatus = "running" | "succeeded" | "failed" | "skipped";
export type JobTrigger = "cron" | "run_now";
export type JobStepStatus = "running" | "succeeded" | "failed";
export type JobErrorCategory =
  | "transient"
  | "recoverable"
  | "fatal"
  | "compliance"
  | "quality";

/** GET /jobs/runs の 1 行(job_history)。 */
export interface JobRun {
  readonly run_id: string; // uuid
  readonly job_name: string;
  readonly status: JobRunStatus;
  readonly trigger?: JobTrigger | null;
  readonly target_date?: string | null; // date
  readonly plan_id?: string | null; // uuid
  readonly error_category?: JobErrorCategory | null;
  readonly error_message?: string | null;
  readonly started_at: string; // date-time
  readonly finished_at?: string | null;
  readonly duration_ms?: number | null;
}

export interface JobRunListResponse {
  readonly items: readonly JobRun[];
}

/** GET /jobs/runs/{run_id}/events の 1 行(job_step_events)。 */
export interface JobStepEvent {
  readonly id: string; // uuid
  readonly run_id: string; // uuid
  readonly step: string;
  readonly status: JobStepStatus;
  readonly genre?: string | null;
  readonly context_type?: string | null;
  readonly context_id?: string | null;
  readonly error_category?: JobErrorCategory | null;
  readonly error_message?: string | null;
  readonly created_at: string; // date-time
}

export interface JobRunEventsResponse {
  readonly run_id: string;
  readonly items: readonly JobStepEvent[];
}

/** 直近の実行一覧を取得する。 */
export async function listJobRuns(options?: {
  readonly limit?: number;
  readonly jobName?: string;
}): Promise<JobRunListResponse> {
  const params = new URLSearchParams();
  if (options?.limit != null) {
    params.set("limit", String(options.limit));
  }
  if (options?.jobName) {
    params.set("job_name", options.jobName);
  }
  const query = params.toString();
  return apiGet<JobRunListResponse>(`/jobs/runs${query ? `?${query}` : ""}`);
}

/** 指定 run の永続化された工程イベントを取得する。 */
export async function listJobRunEvents(
  runId: string,
): Promise<JobRunEventsResponse> {
  return apiGet<JobRunEventsResponse>(`/jobs/runs/${runId}/events`);
}
