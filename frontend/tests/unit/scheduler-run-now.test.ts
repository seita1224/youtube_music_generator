import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  authFetch: vi.fn(),
}));

import { ApiError } from "@/lib/api/client";
import { runNow, runNowConflictMessage } from "@/lib/api/scheduler";
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

describe("runNow", () => {
  it("plan_id を送り 202 の run_id を返す", async () => {
    const planId = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    const runId = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    authFetchMock.mockResolvedValue(
      jsonResponse(
        {
          accepted: true,
          run_id: runId,
          plan_id: planId,
          target_date: "2026-07-11",
        },
        202,
      ),
    );

    const result = await runNow(planId);
    expect(authFetchMock).toHaveBeenCalledWith(
      "/scheduler/run-now",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ plan_id: planId }),
      }),
    );
    expect(result).toEqual({
      accepted: true,
      run_id: runId,
      plan_id: planId,
      target_date: "2026-07-11",
    });
  });
});

describe("runNowConflictMessage", () => {
  it("busy 409 は実行中メッセージ", () => {
    const error = new ApiError(
      409,
      JSON.stringify({ detail: "別の音楽生成が実行中" }),
    );
    expect(runNowConflictMessage(error)).toContain("別の音楽生成が実行中");
  });

  it("Plan-not-approved 409 は未承認メッセージ", () => {
    const error = new ApiError(
      409,
      JSON.stringify({ detail: "Plan is not approved (status=generated)" }),
    );
    expect(runNowConflictMessage(error)).toContain("承認済みではありません");
  });
});
