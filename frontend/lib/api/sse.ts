// T123: backend `/jobs/stream`(SSE / text/event-stream)の購読クライアント。
//
// EventSource は使用不可。 EventSource は Authorization ヘッダを送れないため、
// Basic 認証下の backend(/api/backend 経由 proxy)に到達できない(use-authed-blob-url.ts と同理由)。
// 代わりに authFetch(`@/lib/auth`, Authorization 注入)で fetch し、
// response.body.getReader() で text/event-stream を手パースして onEvent へ渡す。
// 切断/エラー時は指数バックオフで再接続し、 AbortController で停止できる。
//
// 型は contracts/backend-api.yaml の JobEvent schema に手書きで一致させる
// (client.ts / dryrun.ts と同方針: 生成 schema.ts には依存しない)。
// contract には無い `genre`(grid の列キー)は SSE 実装拡張として追加する。

import { authFetch } from "@/lib/auth";

/** ジョブの実行状態。 contract JobEvent.status enum と一致。 */
export type JobStatus = "running" | "succeeded" | "failed";

/** 失敗時の分類。 backend ErrorCategory と一致。 */
export type JobErrorCategory =
  | "transient"
  | "recoverable"
  | "fatal"
  | "compliance"
  | "quality";

/** SSE で配信される 1 イベント(contract JobEvent)。 */
export interface JobEvent {
  readonly timestamp: string; // ISO8601(JST, +09:00)
  readonly run_id?: string; // uuid = job_history.id
  readonly job_name: string; // "music_generation" 等
  readonly step: string; // 進捗軸(cycle/post/music/...)
  readonly status: JobStatus;
  readonly genre?: string | null; // タイムライン表示用(post 単位 step のみ非 null)
  readonly context_type?: string | null; // "plan" / "post"
  readonly context_id?: string | null; // uuid 文字列
  readonly error_category?: JobErrorCategory | null;
  readonly message?: string | null;
}

/** 購読の接続状態(onStatus コールバックで通知)。 */
export type JobStreamStatus = "connecting" | "open" | "reconnecting" | "closed";

/** subscribeJobs のオプション。 */
export interface SubscribeJobsOptions {
  /** 購読対象の実行 ID(job_history.id)。 必須(contract: run_id query)。 */
  readonly runId: string;
  /** 外部からの停止用シグナル(指定時は内部 AbortController とは別系統で停止できる)。 */
  readonly signal?: AbortSignal;
  /** 接続状態の変化を受け取る(EventSource 風の onopen/onerror 相当)。 */
  readonly onStatus?: (status: JobStreamStatus) => void;
}

const STREAM_PATH = "/jobs/stream";
const BACKOFF_INITIAL_MS = 1000;
const BACKOFF_MAX_MS = 30000;

/** 中断由来の例外か(AbortController.abort()/DOMException AbortError)。 */
function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** `data:` 行を JSON.parse して JobEvent として返す。 失敗時は null(無視)。 */
function parseEventData(data: string): JobEvent | null {
  if (!data) {
    return null;
  }
  try {
    return JSON.parse(data) as JobEvent;
  } catch {
    // 壊れたフレームは無視して購読を継続する。
    return null;
  }
}

/**
 * 1 つの SSE フレーム(空行 `\n\n` で区切られたブロック)を処理して onEvent へ渡す。
 * `data:` 行を連結し、 `:` で始まるコメント行(keep-alive ping)は無視する。
 */
function dispatchFrame(frame: string, onEvent: (event: JobEvent) => void): void {
  const dataLines: string[] = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (line.startsWith(":")) {
      continue; // コメント行(ping)。
    }
    if (line.startsWith("data:")) {
      // 仕様上 "data:" 直後の単一スペースのみ除去する。
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  if (dataLines.length === 0) {
    return;
  }
  const event = parseEventData(dataLines.join("\n"));
  if (event) {
    onEvent(event);
  }
}

/**
 * 1 接続分のストリームを読み切る。 正常 EOF で resolve、 abort は呼び出し側で判定。
 * フレームは空行で区切り、 末尾の未完バッファは次チャンクへ持ち越す。
 */
async function readStream(
  response: Response,
  onEvent: (event: JobEvent) => void,
): Promise<void> {
  const body = response.body;
  if (!body) {
    throw new Error("SSE response has no body");
  }
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        dispatchFrame(frame, onEvent);
        separator = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** 指数バックオフ待機。 abort されたら即 reject(AbortError) して再接続を止める。 */
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * backend `/jobs/stream?run_id=...`(SSE)を購読する EventSource 風ラッパ。
 *
 * authFetch 経由(Authorization 付与)で fetch し、 text/event-stream を手パースして
 * onEvent へ JobEvent を渡す。 切断/エラー時は指数バックオフ(1s→上限 30s)で再接続する。
 * 初期表示は DB snapshot(`listJobRunEvents`)、 本関数は以後のライブ差分用。
 *
 * @param onEvent  受信した JobEvent ごとに呼ばれる。
 * @param opts     runId(必須) / signal(外部停止) / onStatus(接続状態通知)。
 * @returns        購読を停止する unsubscribe(AbortController.abort 相当)。
 */
export function subscribeJobs(
  onEvent: (event: JobEvent) => void,
  opts: SubscribeJobsOptions,
): () => void {
  const controller = new AbortController();
  const { runId, signal: externalSignal, onStatus } = opts;
  const streamPath = `${STREAM_PATH}?run_id=${encodeURIComponent(runId)}`;

  const stop = () => controller.abort();
  // 外部 signal が abort されたら内部も停止する。
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener("abort", stop, { once: true });
    }
  }

  const notify = (status: JobStreamStatus) => {
    onStatus?.(status);
  };

  void (async () => {
    let backoff = BACKOFF_INITIAL_MS;
    let attempted = false;
    while (!controller.signal.aborted) {
      notify(attempted ? "reconnecting" : "connecting");
      attempted = true;
      try {
        const response = await authFetch(streamPath, {
          method: "GET",
          headers: { Accept: "text/event-stream" },
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`SSE connect failed: ${response.status}`);
        }
        notify("open");
        backoff = BACKOFF_INITIAL_MS; // 接続成功でバックオフをリセット。
        await readStream(response, onEvent);
        // 正常 EOF。 ループ先頭で abort 判定し、 未中断なら再接続する。
      } catch (error: unknown) {
        if (controller.signal.aborted || isAbortError(error)) {
          break; // 明示停止。 静かに終了する。
        }
        // ネットワーク/HTTP エラー。 バックオフ後に再接続する。
      }
      if (controller.signal.aborted) {
        break;
      }
      try {
        notify("reconnecting");
        await delay(backoff, controller.signal);
      } catch {
        break; // 待機中に abort された。
      }
      backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
    }
    if (externalSignal) {
      externalSignal.removeEventListener("abort", stop);
    }
    notify("closed");
  })();

  return stop;
}
