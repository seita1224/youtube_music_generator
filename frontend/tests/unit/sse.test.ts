import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// lib/api/sse.ts: backend `/jobs/stream`(text/event-stream)の購読ラッパ。
// EventSource ではなく authFetch + ReadableStream 手パースで実装(Authorization 注入のため)。
// authFetch をモックし、 制御可能な ReadableStream を返して以下を検証する:
//   - フレーム分割 / 複数 data 行 / コメント(keep-alive)無視 / 壊れた JSON 無視
//   - チャンク境界をまたぐバッファ持ち越し
//   - 接続失敗時の指数バックオフ再接続
//   - unsubscribe / 外部 signal / ストリーム中の abort で停止し closed 通知

vi.mock("@/lib/auth", () => ({
  authFetch: vi.fn(),
}));

import { subscribeJobs, type JobEvent } from "@/lib/api/sse";
import { authFetch } from "@/lib/auth";

const authFetchMock = vi.mocked(authFetch);
const encoder = new TextEncoder();

/** sse.ts が参照するのは ok / status / body のみ。 最小の Response 風オブジェクトで足りる。 */
function fakeResponse(
  body: ReadableStream<Uint8Array> | null,
  status = 200,
): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    body,
  } as unknown as Response;
}

/** 与えた文字列チャンクを順に流して閉じる有限ストリーム。 */
function streamOf(chunks: readonly string[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

/** データを出さず、 init.signal の abort で read() を reject させるストリーム(接続維持用)。 */
function hangingStreamFor(signal: AbortSignal | null | undefined): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      if (signal) {
        if (signal.aborted) {
          controller.error(new DOMException("Aborted", "AbortError"));
          return;
        }
        signal.addEventListener(
          "abort",
          () => controller.error(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      }
    },
  });
}

/** SSE フレーム文字列(data 行 + 区切り空行)。 */
function frame(event: Partial<JobEvent>): string {
  return `data: ${JSON.stringify(event)}\n\n`;
}

const RUN_ID = "11111111-1111-1111-1111-111111111111";

const SAMPLE: JobEvent = {
  timestamp: "2026-06-22T10:00:00+09:00",
  run_id: RUN_ID,
  job_name: "music_generation",
  step: "music",
  status: "succeeded",
  genre: "lo-fi hip hop",
};

const SUBSCRIBE_OPTS = { runId: RUN_ID } as const;

beforeEach(() => {
  authFetchMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("subscribeJobs: フレーム解析", () => {
  it("run_id クエリ付きで /jobs/stream を購読する", async () => {
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        return fakeResponse(streamOf([frame(SAMPLE)]));
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const stop = subscribeJobs(() => {}, SUBSCRIBE_OPTS);
    await vi.waitFor(() => expect(authFetchMock).toHaveBeenCalled());
    stop();

    const [path] = authFetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe(`/jobs/stream?run_id=${encodeURIComponent(RUN_ID)}`);
  });

  it("2 フレームを解析し onEvent へ渡し、 onStatus は connecting→open", async () => {
    // 1 接続目は 2 イベントの有限ストリーム。 2 接続目以降は abort 可能な無限ストリーム
    //(EOF 後の再接続でテストが先へ進まないようにする)。
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        return fakeResponse(
          streamOf([frame({ ...SAMPLE, step: "cycle", status: "running" }), frame(SAMPLE)]),
        );
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const events: JobEvent[] = [];
    const statuses: string[] = [];
    const stop = subscribeJobs((e) => events.push(e), {
      ...SUBSCRIBE_OPTS,
      onStatus: (s) => statuses.push(s),
    });

    await vi.waitFor(() => expect(events).toHaveLength(2));
    stop();

    expect(events[0]).toMatchObject({ step: "cycle", status: "running" });
    expect(events[1]).toMatchObject({ step: "music", status: "succeeded" });
    expect(statuses[0]).toBe("connecting");
    expect(statuses).toContain("open");
    await vi.waitFor(() => expect(statuses.at(-1)).toBe("closed"));
  });

  it("チャンク境界をまたぐ 1 フレームをバッファ持ち越しで復元する", async () => {
    const whole = frame(SAMPLE);
    const cut = Math.floor(whole.length / 2);
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        // フレームを途中で 2 チャンクに分割して流す。
        return fakeResponse(streamOf([whole.slice(0, cut), whole.slice(cut)]));
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const events: JobEvent[] = [];
    const stop = subscribeJobs((e) => events.push(e), SUBSCRIBE_OPTS);
    await vi.waitFor(() => expect(events).toHaveLength(1));
    stop();
    expect(events[0]).toMatchObject({ step: "music", genre: "lo-fi hip hop" });
  });

  it("コメント行(keep-alive)と壊れた JSON フレームは無視する", async () => {
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        return fakeResponse(
          streamOf([
            ": keep-alive ping\n\n", // コメントのみ → イベント無し
            "data: {not valid json}\n\n", // パース失敗 → 無視
            frame(SAMPLE), // 正常 → 1 件
          ]),
        );
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const events: JobEvent[] = [];
    const stop = subscribeJobs((e) => events.push(e), SUBSCRIBE_OPTS);
    await vi.waitFor(() => expect(events).toHaveLength(1));
    stop();
    expect(events[0]).toMatchObject({ step: "music", status: "succeeded" });
  });

  it("複数 data 行を改行連結して 1 イベントにする", async () => {
    // JSON を 2 つの data 行に割って送る(SSE 仕様: data 行は \n で連結)。
    const multiline = 'data: {"timestamp":"t","job_name":"daily_cycle",\ndata: "step":"image","status":"running"}\n\n';
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        return fakeResponse(streamOf([multiline]));
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const events: JobEvent[] = [];
    const stop = subscribeJobs((e) => events.push(e), SUBSCRIBE_OPTS);
    await vi.waitFor(() => expect(events).toHaveLength(1));
    stop();
    expect(events[0]).toMatchObject({ step: "image", status: "running" });
  });
});

describe("subscribeJobs: 再接続", () => {
  it("接続失敗(500)後、 バックオフ待機して再接続し open になる", async () => {
    vi.useFakeTimers();
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        return fakeResponse(null, 500); // 非 2xx → throw → 再接続へ
      }
      if (authFetchMock.mock.calls.length === 2) {
        return fakeResponse(streamOf([frame(SAMPLE)]));
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const events: JobEvent[] = [];
    const statuses: string[] = [];
    const stop = subscribeJobs((e) => events.push(e), {
      ...SUBSCRIBE_OPTS,
      onStatus: (s) => statuses.push(s),
    });

    // 1 接続目の解決(500)→ reconnecting → delay(1000) をスケジュール。
    await vi.advanceTimersByTimeAsync(0);
    expect(authFetchMock).toHaveBeenCalledTimes(1);
    // バックオフ(初期 1000ms)経過 → 2 接続目 → open → イベント受信。
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(0);

    expect(authFetchMock).toHaveBeenCalledTimes(2);
    expect(statuses).toContain("reconnecting");
    expect(statuses).toContain("open");
    expect(events).toHaveLength(1);

    stop();
    await vi.advanceTimersByTimeAsync(0);
  });
});

describe("subscribeJobs: 停止", () => {
  it("unsubscribe で再接続ループを止め closed を通知する", async () => {
    // 有限ストリーム(EOF)→ 再接続待機(delay)中に stop() → ループ break → closed。
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (authFetchMock.mock.calls.length === 1) {
        return fakeResponse(streamOf([frame(SAMPLE)]));
      }
      return fakeResponse(hangingStreamFor(signal));
    });

    const statuses: string[] = [];
    const events: JobEvent[] = [];
    const stop = subscribeJobs((e) => events.push(e), {
      ...SUBSCRIBE_OPTS,
      onStatus: (s) => statuses.push(s),
    });

    await vi.waitFor(() => expect(events).toHaveLength(1));
    stop();
    await vi.waitFor(() => expect(statuses.at(-1)).toBe("closed"));
    // EOF→delay 中の停止なので、 再接続(2 回目の authFetch)は起きない。
    expect(authFetchMock).toHaveBeenCalledTimes(1);
  });

  it("ストリーム受信中の abort でも停止し closed を通知する", async () => {
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      // 最初から無限ストリーム = 受信待ち状態で abort を受ける。
      return fakeResponse(hangingStreamFor(signal));
    });

    const statuses: string[] = [];
    const stop = subscribeJobs(() => {}, { ...SUBSCRIBE_OPTS, onStatus: (s) => statuses.push(s) });

    await vi.waitFor(() => expect(statuses).toContain("open"));
    stop();
    await vi.waitFor(() => expect(statuses.at(-1)).toBe("closed"));
  });

  it("既に abort 済みの外部 signal では一度も接続せず closed", async () => {
    authFetchMock.mockImplementation(async (_path, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      return fakeResponse(hangingStreamFor(signal));
    });

    const controller = new AbortController();
    controller.abort();
    const events: JobEvent[] = [];
    const statuses: string[] = [];
    subscribeJobs((e) => events.push(e), {
      ...SUBSCRIBE_OPTS,
      signal: controller.signal,
      onStatus: (s) => statuses.push(s),
    });

    await vi.waitFor(() => expect(statuses).toContain("closed"));
    expect(authFetchMock).not.toHaveBeenCalled();
    expect(events).toHaveLength(0);
  });
});
