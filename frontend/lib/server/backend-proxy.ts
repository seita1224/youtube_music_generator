// backend へのサーバ側プロキシ(BFF)。セッション検証後に Basic Authorization を注入する。
// SSE / 音声 / ダウンロード向けに body をストリーム転送し、必要な応答ヘッダを維持する。

import { backendBasicAuthorization } from "@/lib/server/credentials";

const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);

/** クライアントへ転送してよい応答ヘッダ。 */
const PASS_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "content-length",
  "cache-control",
  "etag",
  "last-modified",
  "accept-ranges",
  "content-range",
  "x-accel-buffering",
];

export function backendBaseUrl(): string {
  return (process.env.BACKEND_BASE_URL ?? "http://127.0.0.1:8000").replace(
    /\/+$/,
    "",
  );
}

/**
 * path セグメントが危険(`.` / `..` / 空 / 区切り文字)なら true。
 * percent-encoding の正規化後も検査する。
 */
export function isUnsafeBackendPathSegment(segment: string): boolean {
  let current = segment;
  // 二重エンコード(%252e 等)を数回ほどほどく。
  for (let i = 0; i < 4; i += 1) {
    if (
      current === "." ||
      current === ".." ||
      current === "" ||
      current.includes("/") ||
      current.includes("\\") ||
      current.includes("\0")
    ) {
      return true;
    }
    try {
      const next = decodeURIComponent(current.replace(/\+/g, " "));
      if (next === current) {
        break;
      }
      current = next;
    } catch {
      return true;
    }
  }
  return (
    current === "." ||
    current === ".." ||
    current === "" ||
    current.includes("/") ||
    current.includes("\\") ||
    current.includes("\0")
  );
}

function buildTargetUrl(pathSegments: string[], requestUrl: string): string {
  const incoming = new URL(requestUrl);
  const path = pathSegments.map(encodeURIComponent).join("/");
  const target = new URL(`${backendBaseUrl()}/${path}`);
  target.search = incoming.search;
  return target.toString();
}

function buildUpstreamHeaders(request: Request): Headers {
  const headers = new Headers();
  request.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower)) {
      return;
    }
    if (lower === "authorization" || lower === "cookie") {
      return;
    }
    // Accept-Encoding は下で identity 固定する。
    if (lower === "accept-encoding") {
      return;
    }
    headers.set(key, value);
  });
  // Content-Encoding 付き応答を中継ミスしないよう、非圧縮で取得する。
  headers.set("Accept-Encoding", "identity");
  const basic = backendBasicAuthorization();
  if (basic) {
    headers.set("Authorization", basic);
  }
  return headers;
}

function buildDownstreamHeaders(upstream: Response): Headers {
  const headers = new Headers();
  for (const name of PASS_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) {
      headers.set(name, value);
    }
  }
  // SSE のプロキシ buffering 抑制
  const contentType = upstream.headers.get("content-type") ?? "";
  if (contentType.includes("text/event-stream")) {
    headers.set("Cache-Control", "no-cache, no-transform");
    headers.set("X-Accel-Buffering", "no");
  }
  return headers;
}

/**
 * `/api/backend/*` を backend へ転送する。
 * @param pathSegments catch-all の path 配列(例: ["jobs", "stream"])
 */
export async function proxyToBackend(
  request: Request,
  pathSegments: string[],
): Promise<Response> {
  if (pathSegments.some(isUnsafeBackendPathSegment)) {
    return Response.json({ detail: "Not Found" }, { status: 404 });
  }

  const method = request.method.toUpperCase();
  const target = buildTargetUrl(pathSegments, request.url);
  const headers = buildUpstreamHeaders(request);

  const init: RequestInit = {
    method,
    headers,
    redirect: "manual",
    // Node fetch: duplex が必要なストリーム body 向け
    cache: "no-store",
  };

  if (method !== "GET" && method !== "HEAD") {
    // Request body をそのまま転送(JSON / multipart / 空)。
    init.body = request.body;
    // Node.js undici は stream body に duplex: 'half' が必要
    (init as RequestInit & { duplex?: "half" }).duplex = "half";
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    return Response.json(
      { detail: "Backend unavailable" },
      { status: 502 },
    );
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: buildDownstreamHeaders(upstream),
  });
}
