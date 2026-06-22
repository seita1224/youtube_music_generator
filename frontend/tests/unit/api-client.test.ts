import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// lib/api/client.ts: authFetch 上の薄い JSON クライアント(apiGet/apiPut/apiPost)。
// authFetch をモックし、 メソッド/ヘッダ/ボディの組み立てと、 非 2xx → ApiError を検証する。

vi.mock("@/lib/auth", () => ({
  authFetch: vi.fn(),
}));

import { ApiError, apiGet, apiPost, apiPut } from "@/lib/api/client";
import { authFetch } from "@/lib/auth";

const authFetchMock = vi.mocked(authFetch);

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** authFetch 最終呼び出しの (path, init) を取り出す。 */
function lastCall(): { path: string; init: RequestInit } {
  const call = authFetchMock.mock.calls.at(-1);
  if (!call) {
    throw new Error("authFetch was not called");
  }
  const [path, init] = call as [string, RequestInit];
  return { path, init: init ?? {} };
}

beforeEach(() => {
  authFetchMock.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("apiGet", () => {
  it("2xx の JSON ボディを型 T として返す", async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ enabled: true }));
    const result = await apiGet<{ enabled: boolean }>("/scheduler");
    expect(result).toEqual({ enabled: true });
  });

  it("method GET で authFetch を呼ぶ", async () => {
    authFetchMock.mockResolvedValue(jsonResponse({}));
    await apiGet("/health");
    const { path, init } = lastCall();
    expect(path).toBe("/health");
    expect(init.method).toBe("GET");
  });

  it("非 2xx は ApiError(status + body 文言)を throw する", async () => {
    authFetchMock.mockResolvedValue(
      new Response("not found detail", { status: 404, statusText: "Not Found" }),
    );
    await expect(apiGet("/missing")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "not found detail",
    });
  });

  it("ボディが空の非 2xx は `status statusText` を文言にする", async () => {
    authFetchMock.mockResolvedValue(
      new Response("", { status: 500, statusText: "Internal Server Error" }),
    );
    await expect(apiGet("/boom")).rejects.toThrow(/500 Internal Server Error/);
  });

  it("ApiError は instanceof Error / ApiError を満たす", async () => {
    authFetchMock.mockResolvedValue(new Response("x", { status: 400 }));
    const error = await apiGet("/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
  });
});

describe("空ボディ応答(apiPost<void> 等)", () => {
  it("204 No Content は undefined を返す(json() で落ちない)", async () => {
    authFetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiPost<void>("/dryrun/outputs/1/reject", { reason: "理由" }))
      .resolves.toBeUndefined();
  });

  it("200 でも空ボディなら undefined を返す", async () => {
    authFetchMock.mockResolvedValue(new Response("", { status: 200 }));
    await expect(apiPut<void>("/llm/providers", {})).resolves.toBeUndefined();
  });
});

describe("apiPut", () => {
  it("method PUT + Content-Type: application/json + JSON body を送る", async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ dryrun_enabled: true }));
    const result = await apiPut<{ dryrun_enabled: boolean }>(
      "/scheduler/mode",
      { dryrun_enabled: true },
    );
    expect(result).toEqual({ dryrun_enabled: true });

    const { path, init } = lastCall();
    expect(path).toBe("/scheduler/mode");
    expect(init.method).toBe("PUT");
    expect(new Headers(init.headers).get("Content-Type")).toBe(
      "application/json",
    );
    expect(init.body).toBe(JSON.stringify({ dryrun_enabled: true }));
  });
});

describe("apiPost", () => {
  it("body 有り: Content-Type を付け JSON body を送る", async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ updated_count: 2 }));
    await apiPost("/scheduler/panic-stop", { window_hours: 24 });

    const { init } = lastCall();
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe(
      "application/json",
    );
    expect(init.body).toBe(JSON.stringify({ window_hours: 24 }));
  });

  it("body 無し: Content-Type を付けず body は undefined", async () => {
    authFetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await apiPost("/some/action");

    const { init } = lastCall();
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).has("Content-Type")).toBe(false);
    expect(init.body).toBeUndefined();
  });
});
