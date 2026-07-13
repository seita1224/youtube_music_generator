import { defineConfig, devices } from "@playwright/test";

// E2E: 管理 UI の critical user flow(dryrun レビュー US2 / scheduler US4 / llm US5 /
// jobs SSE US6)。 backend はすべて page.route でモックするため実 backend 不要。
//
// webServer で `next dev` を専用ポート(既定 3100)で自動起動し、 自己完結・CI 実行可能にする
// (docker frontend の 3000/3001 と衝突しないようポートを分離)。 reuseExistingServer は
// ローカルで既存 dev を再利用、 CI では毎回起動する。
const E2E_PORT = process.env.E2E_PORT ?? "3100";
const BASE_URL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${E2E_PORT}`;

const E2E_ADMIN_USER =
  process.env.E2E_ADMIN_USER ?? process.env.ADMIN_USERNAME ?? "admin";
const E2E_ADMIN_PASSWORD =
  process.env.E2E_ADMIN_PASSWORD ?? process.env.ADMIN_PASSWORD ?? "admin";
const E2E_SESSION_SECRET =
  process.env.AUTH_SESSION_SECRET ??
  "e2e-only-auth-session-secret-not-for-production-use";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 4,
  // next dev はルート初回アクセス時にオンデマンドコンパイルするため、 既定より長めに待つ。
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npm run dev -- --port ${E2E_PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
    env: {
      ...process.env,
      ADMIN_USERNAME: E2E_ADMIN_USER,
      ADMIN_PASSWORD: E2E_ADMIN_PASSWORD,
      AUTH_SESSION_SECRET: E2E_SESSION_SECRET,
      AUTH_COOKIE_SECURE: "false",
      BACKEND_BASE_URL: process.env.BACKEND_BASE_URL ?? "http://127.0.0.1:8000",
    },
  },
});
