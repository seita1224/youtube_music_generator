// Jobs UI のタイムライン合成ヘルパ。
// 初期スナップショットの正本は REST GET /jobs/runs/{id}/events。
// SSE は backend が接続時に snapshot を再送するため、 同一 fingerprint はライブ側から除外する。

import type { JobRunStatus, JobStepEvent } from "@/lib/api/jobs";
import type { JobEvent } from "@/lib/api/sse";

/**
 * fingerprint 用に ISO 時刻を安定な epoch(ms) 文字列へ正規化する。
 * 同一瞬間の `Z` / `+00:00` / `+09:00` 等は一致する。
 * 不正・空は生文字列をそのまま返す(決定的フォールバック)。
 */
export function normalizeTimestampForFingerprint(raw: string): string {
  if (raw === "") {
    return "";
  }
  const ms = Date.parse(raw);
  if (Number.isNaN(ms)) {
    return raw;
  }
  return String(ms);
}

/** REST / SSE 共通の重複判定キー(id は使わない: SSE に id が無い)。 */
export function eventFingerprint(event: {
  readonly step: string;
  readonly status: string;
  readonly genre?: string | null;
  readonly context_id?: string | null;
  readonly created_at?: string;
  readonly timestamp?: string;
}): string {
  const rawTs = event.created_at ?? event.timestamp ?? "";
  return [
    event.step,
    event.status,
    event.genre ?? "",
    event.context_id ?? "",
    normalizeTimestampForFingerprint(rawTs),
  ].join("|");
}

/** REST snapshot に既にあるイベントを SSE ライブ列から除外する。 */
export function filterLiveAgainstSnapshot(
  liveEvents: readonly JobEvent[],
  snapshotEvents: readonly JobStepEvent[],
): readonly JobEvent[] {
  const snapshotKeys = new Set(
    snapshotEvents.map((event) => eventFingerprint(event)),
  );
  return liveEvents.filter(
    (event) => !snapshotKeys.has(eventFingerprint(event)),
  );
}

/**
 * running → 終端(succeeded/failed/skipped)へ遷移したとき、
 * 永続イベントを再取得すべきか。
 */
export function shouldRefetchEventsOnTerminal(
  previousStatus: JobRunStatus | null | undefined,
  nextStatus: JobRunStatus | null | undefined,
): boolean {
  return (
    previousStatus === "running" &&
    nextStatus != null &&
    nextStatus !== "running"
  );
}
