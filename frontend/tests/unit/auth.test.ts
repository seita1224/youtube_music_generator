import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authFetch, basicAuthHeader } from "@/lib/auth";

// lib/auth.ts: Basic 認証ヘッダ注入の fetch wrapper(T065 / ADR-0013)。
//  - basicAuthHeader: env クレデンシャルから `Basic base64(user:pass)` を組む
//  - authFetch: 相対パスを `/api/backend` 配下へ正規化し、 Authorization を注入する
//
// global fetch をスタブし、 呼び出し URL と headers を検査する。 クレデンシャルは
// process.env を呼び出し時に読むため(readCredentials)、 テストごとに設定/解除する。

const USER_ENV = "NEXT_PUBLIC_BASIC_AUTH_USER";
const PASS_ENV = "NEXT_PUBLIC_BASIC_AUTH_PASSWORD";

function setCredentials(user: string | null, password: string | null): void {
  if (user === null) {
    delete process.env[USER_ENV];
  } else {
    process.env[USER_ENV] = user;
  }
  if (password === null) {
    delete process.env[PASS_ENV];
  } else {
    process.env[PASS_ENV] = password;
  }
}

/** 最後の fetch 呼び出しの (url, init.headers) を取り出す。 */
function lastFetchCall(fetchMock: ReturnType<typeof vi.fn>): {
  url: string;
  headers: Headers;
} {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) {
    throw new Error("fetch was not called");
  }
  const [url, init] = call as [string, RequestInit | undefined];
  return { url, headers: new Headers(init?.headers) };
}

describe("basicAuthHeader", () => {
  const originalUser = process.env[USER_ENV];
  const originalPass = process.env[PASS_ENV];

  afterEach(() => {
    setCredentials(originalUser ?? null, originalPass ?? null);
  });

  it("env が揃っていれば Basic base64(user:pass) を返す", () => {
    setCredentials("admin", "secret");
    const expected = `Basic ${Buffer.from("admin:secret").toString("base64")}`;
    expect(basicAuthHeader()).toBe(expected);
  });

  it("非 ASCII クレデンシャルも UTF-8 base64 で正しくエンコードする", () => {
    setCredentials("管理", "パス");
    const expected = `Basic ${Buffer.from("管理:パス", "utf-8").toString("base64")}`;
    expect(basicAuthHeader()).toBe(expected);
  });

  it("env が欠けていれば null(ブラウザの Basic セッションに委ねる)", () => {
    setCredentials("admin", null);
    expect(basicAuthHeader()).toBeNull();
    setCredentials(null, "secret");
    expect(basicAuthHeader()).toBeNull();
    setCredentials(null, null);
    expect(basicAuthHeader()).toBeNull();
  });
});

describe("[FR-085] authFetch (Basic 認証ヘッダ注入)", () => {
  const originalUser = process.env[USER_ENV];
  const originalPass = process.env[PASS_ENV];
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response("ok", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setCredentials(originalUser ?? null, originalPass ?? null);
  });

  it("相対パスを /api/backend 配下へ正規化する", async () => {
    setCredentials(null, null);
    await authFetch("/health");
    expect(lastFetchCall(fetchMock).url).toBe("/api/backend/health");
  });

  it("先頭スラッシュ無しのパスも正規化する", async () => {
    setCredentials(null, null);
    await authFetch("scheduler");
    expect(lastFetchCall(fetchMock).url).toBe("/api/backend/scheduler");
  });

  it("既に /api/backend 前置のパスは二重付与しない", async () => {
    setCredentials(null, null);
    await authFetch("/api/backend/llm/usage");
    expect(lastFetchCall(fetchMock).url).toBe("/api/backend/llm/usage");
  });

  it("絶対 URL はそのまま渡す", async () => {
    setCredentials(null, null);
    await authFetch("https://example.test/x");
    expect(lastFetchCall(fetchMock).url).toBe("https://example.test/x");
  });

  it("クレデンシャル設定時は Authorization を注入する", async () => {
    setCredentials("admin", "secret");
    await authFetch("/health");
    const { headers } = lastFetchCall(fetchMock);
    expect(headers.get("Authorization")).toBe(
      `Basic ${Buffer.from("admin:secret").toString("base64")}`,
    );
  });

  it("クレデンシャル未設定時は Authorization を付けない", async () => {
    setCredentials(null, null);
    await authFetch("/health");
    expect(lastFetchCall(fetchMock).headers.has("Authorization")).toBe(false);
  });

  it("呼び出し側が指定した Authorization を上書きしない", async () => {
    setCredentials("admin", "secret");
    await authFetch("/health", { headers: { Authorization: "Bearer caller" } });
    expect(lastFetchCall(fetchMock).headers.get("Authorization")).toBe(
      "Bearer caller",
    );
  });
});
