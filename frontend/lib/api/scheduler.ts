// backend `/scheduler` の型付きクライアント(contracts/backend-api.yaml の SchedulerState /
// panic-stop / scheduler/mode)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる(health.ts / dryrun.ts と同方針)。
// US3(dryrun/投稿モード切替)と US4(緊急停止)で共用する。

import { apiGet, apiPost, apiPut } from "@/lib/api/client";

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
