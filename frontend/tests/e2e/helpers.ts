// E2E 共有ヘルパー。 testMatch(*.spec.ts)に該当しないため test として実行されない。
//
// ADR-0013: frontend セッション認証。 httpCredentials(Basic)は使わない。
// page.request でログインし、ブラウザコンテキストへ cookie を載せる。

import { type Page } from "@playwright/test";

export const ADMIN_USER =
  process.env.E2E_ADMIN_USER ??
  process.env.ADMIN_USERNAME ??
  "admin";

export const ADMIN_PASSWORD =
  process.env.E2E_ADMIN_PASSWORD ??
  process.env.ADMIN_PASSWORD ??
  "admin";

/** ログイン API でセッション cookie を page のブラウザコンテキストに載せる。 */
export async function loginViaApi(page: Page): Promise<void> {
  const base = process.env.E2E_BASE_URL ?? "http://127.0.0.1:3100";
  const response = await page.request.post("/api/auth/login", {
    headers: {
      // Playwright APIRequest は Origin を付けないことがある → CSRF 用に明示。
      Origin: base,
      "Content-Type": "application/json",
    },
    data: { username: ADMIN_USER, password: ADMIN_PASSWORD },
  });
  if (!response.ok()) {
    throw new Error(`loginViaApi failed: ${response.status()}`);
  }
}

/** ログインフォーム経由で認証する。 */
export async function loginViaUi(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("login-form").waitFor({ state: "visible" });
  await page.waitForSelector('[data-testid="login-form"][data-hydrated="true"]');
  await page.getByTestId("login-username").fill(ADMIN_USER);
  await page.getByTestId("login-password").fill(ADMIN_PASSWORD);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.startsWith("/login"), {
      timeout: 20_000,
    }),
    page.getByTestId("login-submit").click(),
  ]);
}
