import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authFetch } from "@/lib/auth";
import { safeNextPath } from "@/lib/safe-next-path";
import {
  createSessionToken,
  MIN_SESSION_SECRET_BYTES,
  SESSION_MAX_AGE_SEC,
  sessionCookieOptions,
  verifySessionToken,
} from "@/lib/server/session";
import {
  GLOBAL_LOGIN_BUCKET_KEY,
  clearLoginAttempts,
  isLoginRateLimited,
  LOGIN_MAX_ATTEMPTS,
  recordLoginFailure,
  resetLoginRateLimit,
} from "@/lib/server/rate-limit";
import { timingSafeStringEqual, verifyAdminCredentials } from "@/lib/server/credentials";
import { assertCsrfForUnsafe, isSameOriginRequest } from "@/lib/server/csrf";
import {
  isUnsafeBackendPathSegment,
  proxyToBackend,
} from "@/lib/server/backend-proxy";

// ADR-0013: セッション認証 + BFF。 クライアントに Basic 資格情報を埋め込まない。

const SECRET_ENV = "AUTH_SESSION_SECRET";
const USER_ENV = "ADMIN_USERNAME";
const PASS_ENV = "ADMIN_PASSWORD";
const PUBLIC_USER = "NEXT_PUBLIC_BASIC_AUTH_USER";
const PUBLIC_PASS = "NEXT_PUBLIC_BASIC_AUTH_PASSWORD";

function setEnv(key: string, value: string | null): void {
  if (value === null) {
    delete process.env[key];
  } else {
    process.env[key] = value;
  }
}

describe("[FR-085] authFetch (same-origin cookie)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response("ok", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setEnv(PUBLIC_USER, "leaked");
    setEnv(PUBLIC_PASS, "leaked");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setEnv(PUBLIC_USER, null);
    setEnv(PUBLIC_PASS, null);
  });

  it("相対パスを /api/backend 配下へ正規化する", async () => {
    await authFetch("/health");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/backend/health");
    expect(init.credentials).toBe("same-origin");
  });

  it("Authorization ヘッダを自動注入しない", async () => {
    await authFetch("/health");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });

  it("絶対 URL はそのまま渡す", async () => {
    await authFetch("https://example.test/x");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("https://example.test/x");
  });
});

describe("safeNextPath (open redirect)", () => {
  const base = "http://127.0.0.1:3000/login";

  it("同一オリジン相対パスを許可する", () => {
    expect(safeNextPath("/scheduler", base)).toBe("/scheduler");
    expect(safeNextPath("/jobs?x=1", base)).toBe("/jobs?x=1");
  });

  it.each([
    "/\\evil.com",
    "/\\\\evil.com",
    "//evil.com",
    "/%5Cevil.com",
    "/%2F%2Fevil.com",
    "https://evil.com",
    "//evil.com/phish",
    "\\\\\\evil.com",
  ])("危険な next を拒否する: %s", (candidate) => {
    expect(safeNextPath(candidate, base)).toBe("/");
  });

  it("制御文字を拒否する", () => {
    expect(safeNextPath("/ok\n/evil", base)).toBe("/");
    expect(safeNextPath("/ok\u0000", base)).toBe("/");
  });
});

describe("session token", () => {
  const originalSecret = process.env[SECRET_ENV];

  beforeEach(() => {
    setEnv(SECRET_ENV, "unit-test-session-secret-at-least-32-bytes");
  });

  afterEach(() => {
    setEnv(SECRET_ENV, originalSecret ?? null);
  });

  it("署名付きトークンを検証できる", async () => {
    const token = await createSessionToken("admin", 1_000_000);
    const payload = await verifySessionToken(token, 1_000_000);
    expect(payload).toEqual({
      username: "admin",
      exp: 1_000_000 + SESSION_MAX_AGE_SEC,
    });
  });

  it("改ざんされた署名を拒否する", async () => {
    const token = await createSessionToken("admin", 1_000_000);
    const [body] = token.split(".");
    const bad = `${body}.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`;
    expect(await verifySessionToken(bad, 1_000_000)).toBeNull();
  });

  it("期限切れを拒否する", async () => {
    const token = await createSessionToken("admin", 1_000_000);
    expect(
      await verifySessionToken(token, 1_000_000 + SESSION_MAX_AGE_SEC + 1),
    ).toBeNull();
  });

  it("空・不正形式・壊れた base64 を null で返す(例外なし)", async () => {
    expect(await verifySessionToken(null)).toBeNull();
    expect(await verifySessionToken("")).toBeNull();
    expect(await verifySessionToken("no-dot")).toBeNull();
    expect(await verifySessionToken("%%%not-b64%%%.%%%bad%%%")).toBeNull();
    expect(await verifySessionToken("YWJj.!!!not-base64!!!")).toBeNull();
  });

  it("弱い / 欠落 SECRET では create が失敗し verify は null", async () => {
    setEnv(SECRET_ENV, null);
    await expect(createSessionToken("admin")).rejects.toThrow(
      /AUTH_SESSION_SECRET/,
    );
    expect(await verifySessionToken("a.b")).toBeNull();

    setEnv(SECRET_ENV, "short");
    expect(new TextEncoder().encode("short").byteLength).toBeLessThan(
      MIN_SESSION_SECRET_BYTES,
    );
    await expect(createSessionToken("admin")).rejects.toThrow(/32/);
    expect(await verifySessionToken("a.b")).toBeNull();
  });

  it("cookie オプションは HttpOnly / SameSite=Lax / path=/", () => {
    setEnv("AUTH_COOKIE_SECURE", "false");
    const opts = sessionCookieOptions();
    expect(opts.httpOnly).toBe(true);
    expect(opts.sameSite).toBe("lax");
    expect(opts.path).toBe("/");
    expect(opts.secure).toBe(false);
    expect(opts.maxAge).toBe(SESSION_MAX_AGE_SEC);
  });
});

describe("credentials", () => {
  const originalUser = process.env[USER_ENV];
  const originalPass = process.env[PASS_ENV];

  afterEach(() => {
    setEnv(USER_ENV, originalUser ?? null);
    setEnv(PASS_ENV, originalPass ?? null);
  });

  it("timingSafeStringEqual は一致/不一致を正しく返す", () => {
    expect(timingSafeStringEqual("a", "a")).toBe(true);
    expect(timingSafeStringEqual("a", "b")).toBe(false);
    expect(timingSafeStringEqual("short", "longer")).toBe(false);
  });

  it("verifyAdminCredentials は ADMIN_* と照合する", () => {
    setEnv(USER_ENV, "admin");
    setEnv(PASS_ENV, "s3cret");
    expect(verifyAdminCredentials("admin", "s3cret")).toBe(true);
    expect(verifyAdminCredentials("admin", "wrong")).toBe(false);
    expect(verifyAdminCredentials("other", "s3cret")).toBe(false);
  });
});

describe("login rate limit (global bucket)", () => {
  beforeEach(() => {
    resetLoginRateLimit();
  });

  it("上限超過で制限する", () => {
    const key = GLOBAL_LOGIN_BUCKET_KEY;
    const now = 1_000;
    for (let i = 0; i < LOGIN_MAX_ATTEMPTS; i += 1) {
      expect(isLoginRateLimited(key, now)).toBe(false);
      recordLoginFailure(key, now);
    }
    expect(isLoginRateLimited(key, now)).toBe(true);
    clearLoginAttempts(key);
    expect(isLoginRateLimited(key, now)).toBe(false);
  });

  it("X-Forwarded-For を別バケットとして信頼しない(グローバル共有)", () => {
    const now = 2_000;
    for (let i = 0; i < LOGIN_MAX_ATTEMPTS; i += 1) {
      recordLoginFailure(GLOBAL_LOGIN_BUCKET_KEY, now);
    }
    // クライアントが別 IP を名乗っても同一グローバル制限が掛かる。
    expect(isLoginRateLimited(GLOBAL_LOGIN_BUCKET_KEY, now)).toBe(true);
    expect(isLoginRateLimited("spoofed-via-xff", now)).toBe(false);
  });
});

describe("csrf same-origin", () => {
  it("Origin host 一致を許可する(LAN/local)", () => {
    const req = new Request("http://192.168.1.10:3000/api/auth/login", {
      method: "POST",
      headers: {
        host: "192.168.1.10:3000",
        origin: "http://192.168.1.10:3000",
      },
    });
    expect(isSameOriginRequest(req)).toBe(true);
    expect(assertCsrfForUnsafe(req)).toBeNull();
  });

  it("クロスオリジンを拒否する", () => {
    const req = new Request("http://127.0.0.1:3000/api/backend/x", {
      method: "POST",
      headers: {
        host: "127.0.0.1:3000",
        origin: "http://evil.example",
      },
    });
    expect(isSameOriginRequest(req)).toBe(false);
    const res = assertCsrfForUnsafe(req);
    expect(res?.status).toBe(403);
  });

  it("GET は CSRF チェック対象外", () => {
    const req = new Request("http://127.0.0.1:3000/api/backend/x", {
      method: "GET",
      headers: { host: "127.0.0.1:3000" },
    });
    expect(assertCsrfForUnsafe(req)).toBeNull();
  });

  it("login POST も Origin 欠落を拒否する", () => {
    const req = new Request("http://127.0.0.1:3000/api/auth/login", {
      method: "POST",
      headers: { host: "127.0.0.1:3000" },
    });
    expect(assertCsrfForUnsafe(req)?.status).toBe(403);
  });
});

describe("BFF path segments", () => {
  it.each([".", "..", "%2e", "%2e%2e", "%252e", "..%2f", "foo%2fbar"])(
    "危険セグメントを拒否: %s",
    (segment) => {
      expect(isUnsafeBackendPathSegment(segment)).toBe(true);
    },
  );

  it("通常セグメントは許可", () => {
    expect(isUnsafeBackendPathSegment("health")).toBe(false);
    expect(isUnsafeBackendPathSegment("jobs")).toBe(false);
  });
});

describe("BFF proxyToBackend", () => {
  const originalUser = process.env[USER_ENV];
  const originalPass = process.env[PASS_ENV];
  const originalBackend = process.env.BACKEND_BASE_URL;

  beforeEach(() => {
    setEnv(USER_ENV, "admin");
    setEnv(PASS_ENV, "s3cret");
    setEnv("BACKEND_BASE_URL", "http://backend.test");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setEnv(USER_ENV, originalUser ?? null);
    setEnv(PASS_ENV, originalPass ?? null);
    setEnv("BACKEND_BASE_URL", originalBackend ?? null);
  });

  it("Basic Authorization をサーバ側で注入し query を転送する", async () => {
    const fetchMock = vi.fn(async () => new Response('{"ok":true}', {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const req = new Request("http://127.0.0.1:3000/api/backend/health?x=1", {
      method: "GET",
    });
    const res = await proxyToBackend(req, ["health"]);
    expect(res.status).toBe(200);
    expect(fetchMock.mock.calls[0]).toBeTruthy();
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const [url, init] = call;
    expect(url).toBe("http://backend.test/health?x=1");
    const headers = new Headers(init.headers);
    const auth = headers.get("Authorization");
    expect(auth).toBe(
      `Basic ${Buffer.from("admin:s3cret", "utf8").toString("base64")}`,
    );
    expect(headers.get("Accept-Encoding")).toBe("identity");
  });

  it(". / .. セグメントは upstream へ出さず 404", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const res = await proxyToBackend(
      new Request("http://127.0.0.1:3000/api/backend/%2e%2e/secret"),
      ["%2e%2e", "secret"],
    );
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("POST body を転送する", async () => {
    const fetchMock = vi.fn(async (_url, init) => {
      const body = init?.body;
      return new Response(String(body ?? ""), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const req = new Request("http://127.0.0.1:3000/api/backend/scheduler", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: true }),
    });
    await proxyToBackend(req, ["scheduler"]);
    expect(fetchMock.mock.calls[0]).toBeTruthy();
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("POST");
    expect(init.body).toBeTruthy();
  });

  it("SSE content-type と body ストリームを維持する", async () => {
    const payload = "data: hi\n\n";
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(payload));
        controller.close();
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    );
    const res = await proxyToBackend(
      new Request("http://127.0.0.1:3000/api/backend/jobs/stream"),
      ["jobs", "stream"],
    );
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    expect(res.headers.get("x-accel-buffering")).toBe("no");
    expect(await res.text()).toBe(payload);
  });

  it("audio / Content-Disposition と body を維持する", async () => {
    const bytes = new Uint8Array([1, 2, 3, 4]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "audio/wav",
            "Content-Disposition": 'attachment; filename="track.wav"',
          },
        }),
      ),
    );
    const res = await proxyToBackend(
      new Request("http://127.0.0.1:3000/api/backend/posts/1/tracks/0/download"),
      ["posts", "1", "tracks", "0", "download"],
    );
    expect(res.headers.get("content-type")).toBe("audio/wav");
    expect(res.headers.get("content-disposition")).toContain("track.wav");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(bytes);
  });
});
