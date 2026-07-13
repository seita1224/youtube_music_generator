import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  authFetch: vi.fn(),
}));

import { listJobRunEvents, listJobRuns } from "@/lib/api/jobs";
import { authFetch } from "@/lib/auth";

const authFetchMock = vi.mocked(authFetch);

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  authFetchMock.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("listJobRuns", () => {
  it("limit / job_name クエリ付きで GET /jobs/runs する", async () => {
    authFetchMock.mockResolvedValue(
      jsonResponse({
        items: [
          {
            run_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            job_name: "music_generation",
            status: "succeeded",
            trigger: "run_now",
            target_date: "2026-07-11",
            plan_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            started_at: "2026-07-11T07:00:00+09:00",
            finished_at: "2026-07-11T07:05:00+09:00",
            duration_ms: 300_000,
          },
        ],
      }),
    );

    const result = await listJobRuns({
      limit: 20,
      jobName: "music_generation",
    });

    expect(authFetchMock).toHaveBeenCalledWith(
      "/jobs/runs?limit=20&job_name=music_generation",
      expect.objectContaining({ method: "GET" }),
    );
    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.status).toBe("succeeded");
  });
});

describe("listJobRunEvents", () => {
  it("GET /jobs/runs/{run_id}/events を呼ぶ", async () => {
    const runId = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    authFetchMock.mockResolvedValue(
      jsonResponse({
        run_id: runId,
        items: [
          {
            id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
            run_id: runId,
            step: "music",
            status: "succeeded",
            genre: "lo-fi hip hop",
            created_at: "2026-07-11T07:01:00+09:00",
          },
        ],
      }),
    );

    const result = await listJobRunEvents(runId);
    expect(authFetchMock).toHaveBeenCalledWith(
      `/jobs/runs/${runId}/events`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(result.items[0]?.step).toBe("music");
  });
});
