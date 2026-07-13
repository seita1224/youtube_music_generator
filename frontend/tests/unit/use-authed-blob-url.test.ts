import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// lib/api/use-authed-blob-url.ts: Basic 認証必須メディアを authFetch で取得し object URL 化する hook。
// authFetch と URL.createObjectURL/revokeObjectURL をモックし、 取得→URL 生成→
// アンマウント時の revoke、 path=null や取得失敗時の null 維持を検証する。

vi.mock("@/lib/auth", () => ({
  authFetch: vi.fn(),
}));

import { useAuthedBlobUrl } from "@/lib/api/use-authed-blob-url";
import { authFetch } from "@/lib/auth";

const authFetchMock = vi.mocked(authFetch);

let createObjectURLMock: ReturnType<typeof vi.fn>;
let revokeObjectURLMock: ReturnType<typeof vi.fn>;

function blobResponse(): Response {
  return {
    ok: true,
    blob: async () => new Blob(["fake-bytes"], { type: "image/jpeg" }),
  } as unknown as Response;
}

beforeEach(() => {
  authFetchMock.mockReset();
  createObjectURLMock = vi.fn(() => "blob:mock-url");
  revokeObjectURLMock = vi.fn();
  // jsdom は createObjectURL/revokeObjectURL 未実装のため直接差し込む。
  URL.createObjectURL = createObjectURLMock as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = revokeObjectURLMock as unknown as typeof URL.revokeObjectURL;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useAuthedBlobUrl", () => {
  it("path=null では取得せず null を返す", () => {
    const { result } = renderHook(() => useAuthedBlobUrl(null));
    expect(result.current).toBeNull();
    expect(authFetchMock).not.toHaveBeenCalled();
  });

  it("path 指定で authFetch→blob→object URL を返す", async () => {
    authFetchMock.mockResolvedValue(blobResponse());
    const { result } = renderHook(() =>
      useAuthedBlobUrl("/dryrun/outputs/1/thumbnail"),
    );

    await waitFor(() => expect(result.current).toBe("blob:mock-url"));
    expect(authFetchMock).toHaveBeenCalledWith("/dryrun/outputs/1/thumbnail");
    expect(createObjectURLMock).toHaveBeenCalledTimes(1);
  });

  it("アンマウント時に object URL を revoke する", async () => {
    authFetchMock.mockResolvedValue(blobResponse());
    const { result, unmount } = renderHook(() =>
      useAuthedBlobUrl("/video/1/final.mp4"),
    );
    await waitFor(() => expect(result.current).toBe("blob:mock-url"));

    unmount();
    expect(revokeObjectURLMock).toHaveBeenCalledWith("blob:mock-url");
  });

  it("取得が !ok のときは null のまま(呼び出し側がフォールバック)", async () => {
    authFetchMock.mockResolvedValue({ ok: false } as unknown as Response);
    const { result } = renderHook(() => useAuthedBlobUrl("/forbidden"));

    // 効果の解決を待ってから null を確認する。
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 10));
    expect(result.current).toBeNull();
    expect(createObjectURLMock).not.toHaveBeenCalled();
  });

  it("authFetch が throw しても落ちず null を維持する", async () => {
    authFetchMock.mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useAuthedBlobUrl("/boom"));
    await new Promise((r) => setTimeout(r, 10));
    expect(result.current).toBeNull();
  });

  it("createObjectURL 直後に cancel されたら即 revoke する", async () => {
    authFetchMock.mockResolvedValue(blobResponse());

    let unmountHook: (() => void) | undefined;
    createObjectURLMock.mockImplementation(() => {
      // create の同期中に cleanup が走ると objectUrl 代入前に cancelled になる。
      unmountHook?.();
      return "blob:mock-url";
    });

    const { unmount } = renderHook(() => useAuthedBlobUrl("/race"));
    unmountHook = unmount;

    await waitFor(() => expect(createObjectURLMock).toHaveBeenCalledTimes(1));
    expect(revokeObjectURLMock).toHaveBeenCalledWith("blob:mock-url");
  });
});
