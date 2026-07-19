import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  authFetch: vi.fn(),
}));

import {
  downloadPostTrack,
  listPosts,
  listPostTracks,
  postTrackAudioPath,
  postTrackDownloadPath,
} from "@/lib/api/posts";
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

describe("posts API paths", () => {
  it("再生/DL パスに audio_uri を含めない", () => {
    expect(postTrackAudioPath("post-1", 2)).toBe(
      "/posts/post-1/tracks/2/audio",
    );
    expect(postTrackDownloadPath("post-1", 2)).toBe(
      "/posts/post-1/tracks/2/download",
    );
  });
});

describe("listPosts / listPostTracks", () => {
  it("plan_id で Post 一覧を取得する", async () => {
    authFetchMock.mockResolvedValue(
      jsonResponse({
        items: [
          {
            id: "post-1",
            plan_id: "plan-1",
            position: 0,
            genre: "lo-fi hip hop",
            status: "music_generated",
            created_at: "2026-07-11T07:00:00+09:00",
          },
        ],
      }),
    );

    const result = await listPosts("plan-1");
    expect(authFetchMock).toHaveBeenCalledWith(
      "/posts?plan_id=plan-1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(result.items[0]?.status).toBe("music_generated");
  });

  it("Post のトラック一覧を取得する", async () => {
    authFetchMock.mockResolvedValue(
      jsonResponse({
        items: [
          {
            id: "track-0",
            post_id: "post-1",
            position: 0,
            duration_sec: 60,
            bpm: 84,
            subtheme: "rain",
            acoustid_status: "not_checked",
            generated_at: "2026-07-11T07:01:00+09:00",
          },
        ],
      }),
    );

    const result = await listPostTracks("post-1");
    expect(authFetchMock).toHaveBeenCalledWith(
      "/posts/post-1/tracks",
      expect.objectContaining({ method: "GET" }),
    );
    expect(result.items[0]?.position).toBe(0);
    expect(result.items[0]).not.toHaveProperty("audio_uri");
  });
});

describe("downloadPostTrack", () => {
  it("認証付き blob を取得して download を発火する", async () => {
    const click = vi.fn();
    const remove = vi.fn();
    const appendChild = vi
      .spyOn(document.body, "appendChild")
      .mockImplementation((node) => node);
    const createElement = vi
      .spyOn(document, "createElement")
      .mockImplementation((tag: string) => {
        if (tag === "a") {
          return {
            href: "",
            download: "",
            rel: "",
            click,
            remove,
          } as unknown as HTMLAnchorElement;
        }
        return document.createElement(tag);
      });

    // jsdom は URL.createObjectURL を持たないためスタブする。
    const createObjectURL = vi.fn().mockReturnValue("blob:track");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: revokeObjectURL,
    });

    authFetchMock.mockResolvedValue(
      new Response(new Blob(["wav"]), {
        status: 200,
        headers: {
          "content-type": "audio/wav",
          "content-disposition": 'attachment; filename="track-0.wav"',
        },
      }),
    );

    await downloadPostTrack("post-1", 0);

    expect(authFetchMock).toHaveBeenCalledWith(
      "/posts/post-1/tracks/0/download",
    );
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:track");

    createElement.mockRestore();
    appendChild.mockRestore();
  });
});
