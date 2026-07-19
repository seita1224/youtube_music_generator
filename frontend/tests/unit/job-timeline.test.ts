import { describe, expect, it } from "vitest";

import type { JobStepEvent } from "@/lib/api/jobs";
import {
  eventFingerprint,
  filterLiveAgainstSnapshot,
  shouldRefetchEventsOnTerminal,
} from "@/lib/api/job-timeline";
import type { JobEvent } from "@/lib/api/sse";

function stepEvent(
  overrides: Partial<JobStepEvent> & Pick<JobStepEvent, "id" | "step" | "status">,
): JobStepEvent {
  return {
    run_id: "run-1",
    created_at: "2026-07-11T10:00:01+09:00",
    ...overrides,
  };
}

function liveEvent(
  overrides: Partial<JobEvent> & Pick<JobEvent, "step" | "status" | "timestamp">,
): JobEvent {
  return {
    job_name: "music_generation",
    ...overrides,
  };
}

describe("job-timeline", () => {
  it("REST と SSE で同じ fingerprint になる(id 非依存)", () => {
    const rest = stepEvent({
      id: "e1",
      step: "music",
      status: "running",
      genre: "lo-fi",
      context_id: "post-1",
      created_at: "2026-07-11T10:00:02+09:00",
    });
    const sse = liveEvent({
      step: "music",
      status: "running",
      genre: "lo-fi",
      context_id: "post-1",
      timestamp: "2026-07-11T10:00:02+09:00",
    });
    expect(eventFingerprint(rest)).toBe(eventFingerprint(sse));
  });

  it("同一 timestamp でも status が異なれば fingerprint が異なる", () => {
    const ts = "2026-07-11T10:00:02+09:00";
    const running = stepEvent({
      id: "e1",
      step: "music",
      status: "running",
      genre: "lo-fi",
      context_id: "post-1",
      created_at: ts,
    });
    const succeeded = liveEvent({
      step: "music",
      status: "succeeded",
      genre: "lo-fi",
      context_id: "post-1",
      timestamp: ts,
    });
    expect(eventFingerprint(running)).not.toBe(eventFingerprint(succeeded));
  });

  it("同一瞬間の Z / +00:00 / +09:00 は fingerprint が一致する", () => {
    const base = {
      step: "music" as const,
      status: "running" as const,
      genre: "lo-fi",
      context_id: "post-1",
    };
    // 2026-07-11T01:00:02Z == 2026-07-11T01:00:02+00:00 == 2026-07-11T10:00:02+09:00
    const asZ = eventFingerprint({
      ...base,
      timestamp: "2026-07-11T01:00:02Z",
    });
    const asUtcOffset = eventFingerprint({
      ...base,
      created_at: "2026-07-11T01:00:02+00:00",
    });
    const asJst = eventFingerprint({
      ...base,
      created_at: "2026-07-11T10:00:02+09:00",
    });
    expect(asZ).toBe(asUtcOffset);
    expect(asZ).toBe(asJst);
  });

  it("異なる瞬間の timestamp は fingerprint が異なる", () => {
    const base = {
      step: "music" as const,
      status: "running" as const,
      genre: "lo-fi",
      context_id: "post-1",
    };
    const earlier = eventFingerprint({
      ...base,
      timestamp: "2026-07-11T10:00:02+09:00",
    });
    const later = eventFingerprint({
      ...base,
      timestamp: "2026-07-11T10:00:03+09:00",
    });
    expect(earlier).not.toBe(later);
  });

  it("不正・欠落 timestamp は生文字列フォールバックで決定的", () => {
    const invalidA = eventFingerprint({
      step: "music",
      status: "running",
      timestamp: "not-a-date",
    });
    const invalidB = eventFingerprint({
      step: "music",
      status: "running",
      created_at: "not-a-date",
    });
    expect(invalidA).toBe(invalidB);
    expect(invalidA).toContain("not-a-date");

    const missing = eventFingerprint({
      step: "music",
      status: "running",
    });
    expect(missing).toBe("music|running|||");
  });

  it("オフセット表記が違っても status が異なれば fingerprint が異なる", () => {
    const running = eventFingerprint({
      step: "music",
      status: "running",
      timestamp: "2026-07-11T01:00:02Z",
    });
    const succeeded = eventFingerprint({
      step: "music",
      status: "succeeded",
      created_at: "2026-07-11T10:00:02+09:00",
    });
    expect(running).not.toBe(succeeded);
  });

  it("SSE 初期再送(snapshot 重複)をライブ列から除外する", () => {
    const snapshot = [
      stepEvent({
        id: "e1",
        step: "cycle",
        status: "succeeded",
        created_at: "2026-07-11T10:00:01+09:00",
      }),
      stepEvent({
        id: "e2",
        step: "music",
        status: "running",
        genre: "lo-fi",
        created_at: "2026-07-11T10:00:02+09:00",
      }),
    ];
    const live = [
      liveEvent({
        step: "cycle",
        status: "succeeded",
        timestamp: "2026-07-11T10:00:01+09:00",
      }),
      liveEvent({
        step: "music",
        status: "running",
        genre: "lo-fi",
        timestamp: "2026-07-11T10:00:02+09:00",
      }),
      liveEvent({
        step: "music",
        status: "succeeded",
        genre: "lo-fi",
        timestamp: "2026-07-11T10:00:03+09:00",
      }),
    ];

    const filtered = filterLiveAgainstSnapshot(live, snapshot);
    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.status).toBe("succeeded");
    expect(filtered[0]?.timestamp).toBe("2026-07-11T10:00:03+09:00");
  });

  it("オフセット表記が違う同一瞬間の SSE 再送もライブ列から除外する", () => {
    const snapshot = [
      stepEvent({
        id: "e1",
        step: "music",
        status: "running",
        genre: "lo-fi",
        created_at: "2026-07-11T10:00:02+09:00",
      }),
    ];
    const live = [
      liveEvent({
        step: "music",
        status: "running",
        genre: "lo-fi",
        timestamp: "2026-07-11T01:00:02Z",
      }),
      liveEvent({
        step: "music",
        status: "succeeded",
        genre: "lo-fi",
        timestamp: "2026-07-11T01:00:03+00:00",
      }),
    ];

    const filtered = filterLiveAgainstSnapshot(live, snapshot);
    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.status).toBe("succeeded");
  });

  it("同一 timestamp の running→succeeded はライブ列から潰れない", () => {
    const ts = "2026-07-11T10:00:02+09:00";
    const snapshot = [
      stepEvent({
        id: "e1",
        step: "music",
        status: "running",
        genre: "lo-fi",
        created_at: ts,
      }),
    ];
    const live = [
      liveEvent({
        step: "music",
        status: "running",
        genre: "lo-fi",
        timestamp: ts,
      }),
      liveEvent({
        step: "music",
        status: "succeeded",
        genre: "lo-fi",
        timestamp: ts,
      }),
    ];

    const filtered = filterLiveAgainstSnapshot(live, snapshot);
    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.status).toBe("succeeded");
    expect(filtered[0]?.timestamp).toBe(ts);
  });

  it("running → 終端で永続イベント再取得が必要", () => {
    expect(shouldRefetchEventsOnTerminal("running", "succeeded")).toBe(true);
    expect(shouldRefetchEventsOnTerminal("running", "failed")).toBe(true);
    expect(shouldRefetchEventsOnTerminal("running", "skipped")).toBe(true);
    expect(shouldRefetchEventsOnTerminal("running", "running")).toBe(false);
    expect(shouldRefetchEventsOnTerminal("succeeded", "succeeded")).toBe(false);
    expect(shouldRefetchEventsOnTerminal(null, "succeeded")).toBe(false);
  });
});
