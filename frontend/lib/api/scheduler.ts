// backend `/scheduler` の型付きクライアント(contracts/backend-api.yaml の SchedulerState /
// panic-stop / scheduler/mode)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(health.ts / dryrun.ts と同方針)。
// US3(dryrun/投稿モード切替)と US4(緊急停止)で共用する。

import { ApiError, apiGet, apiPost, apiPut } from "@/lib/api/client";

export interface SchedulerState {
  readonly enabled: boolean;
  readonly updated_at?: string; // date-time
}

// PUT /scheduler/mode 用。 dryrun と投稿モードの切替状態。
// backend に GET /scheduler/mode は無く PUT のみのため、 UI は setMode の戻り値で
// 楽観反映する(getMode は提供しない)。
export interface ModeState {
  readonly dryrun_enabled: boolean;
}

// panic-stop の対象/結果に出る動画行(contracts/backend-api.yaml の Video schema)。
export interface VideoSummary {
  readonly youtube_video_id: string;
  readonly title: string;
  readonly posted_at: string; // date-time
  readonly privacy_status: "public" | "unlisted" | "private" | "deleted";
  readonly contains_synthetic_media: boolean;
  readonly genre?: string;
  readonly duration_sec?: number;
  readonly thumbnail_uri?: string;
}

// POST /scheduler/panic-stop のレスポンス。
// scheduler を停止し、 直近 window 内の動画を列挙、 set_private 指定分を private 化した結果。
export interface PanicStopResult {
  readonly scheduler_enabled: boolean;
  readonly recent_videos: readonly VideoSummary[];
  readonly updated_count: number;
}

/** scheduler の有効/無効状態を取得する。 */
export async function getSchedulerState(): Promise<SchedulerState> {
  return apiGet<SchedulerState>("/scheduler");
}

/** scheduler を有効/無効に切り替える(ADR-0031)。 */
export async function setScheduler(enabled: boolean): Promise<SchedulerState> {
  return apiPut<SchedulerState>("/scheduler", { enabled });
}

/** 投稿モード(dryrun ↔ 本番投稿)を切り替える。 戻り値で UI を楽観反映する。 */
export async function setMode(dryrunEnabled: boolean): Promise<ModeState> {
  return apiPut<ModeState>("/scheduler/mode", { dryrun_enabled: dryrunEnabled });
}

/**
 * コンプラ緊急停止(ADR-0031)。 scheduler を停止し、 直近 windowHours の動画を列挙する。
 * setPrivate(youtube_video_id 配列)を渡すと当該動画を private 化する。 未指定/空は列挙のみ。
 */
export async function panicStop(
  windowHours: number,
  setPrivate?: readonly string[],
): Promise<PanicStopResult> {
  return apiPost<PanicStopResult>("/scheduler/panic-stop", {
    window_hours: windowHours,
    set_private: setPrivate,
  });
}

// POST /scheduler/run-now の 202 レスポンス。
export interface RunNowResponse {
  readonly accepted: boolean;
  readonly run_id: string; // uuid = job_history.id
  readonly plan_id: string; // uuid
  readonly target_date: string; // date YYYY-MM-DD
}

/**
 * 承認済み Daily Plan の音楽生成を即時 1 回起動する(cron 待ち回避)。
 * planId は status=approved 必須。 長時間処理はバックグラウンド(202 + run_id)。
 * 409: 未承認 Plan、 または別の music_generation が running(single-flight)。
 */
export async function runNow(planId: string): Promise<RunNowResponse> {
  return apiPost<RunNowResponse>("/scheduler/run-now", { plan_id: planId });
}

/**
 * run-now の 409 detail を UX 文言へ写像する。
 * busy: 「別の音楽生成が実行中」 / not-approved: 「Plan is not approved」。
 */
export function runNowConflictMessage(error: ApiError): string {
  const detail = extractApiDetail(error.message);
  if (/Plan is not approved|not approved/i.test(detail)) {
    return "選択したプランは承認済みではありません。 プラン一覧で承認してから再試行してください。";
  }
  if (/別の音楽生成が実行中|busy|already running/i.test(detail)) {
    return "別の音楽生成が実行中です。 完了を待ってから再試行してください。";
  }
  // detail が取れない 409 は single-flight を既定とする(運用上多い方)。
  return "別の音楽生成が実行中です。 完了を待ってから再試行してください。";
}

/** ApiError.message(生レスポンス本文)から FastAPI detail 文字列を取り出す。 */
function extractApiDetail(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) {
    return "";
  }
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (typeof parsed === "string") {
      return parsed;
    }
    if (parsed && typeof parsed === "object") {
      const record = parsed as Record<string, unknown>;
      if (typeof record.detail === "string") {
        return record.detail;
      }
      if (typeof record.message === "string") {
        return record.message;
      }
    }
  } catch {
    // 非 JSON はそのまま使う。
  }
  return trimmed;
}
