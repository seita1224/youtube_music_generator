import { test, expect } from "@playwright/test";

import { ADMIN_PASSWORD, ADMIN_USER, loginViaApi, loginViaUi } from "./helpers";

// ADR-0013: ログイン / リダイレクト / ログアウト / 保護 API / CSRF。

test.describe("認証フロー", () => {
  test("未認証の管理画面は /login へリダイレクトする", async ({ page }) => {
    await page.goto("/scheduler");
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByTestId("login-form")).toBeVisible();
    await expect(page).toHaveURL(/next=/);
  });

  test("未認証の /api/backend は 401", async ({ page }) => {
    const res = await page.request.get("/api/backend/health");
    expect(res.status()).toBe(401);
  });

  test("未認証の /api/auth/me は 401", async ({ page }) => {
    const res = await page.request.get("/api/auth/me");
    expect(res.status()).toBe(401);
  });

  test("改ざん cookie は API 401 / ページは /login", async ({ page, context }) => {
    await context.addCookies([
      {
        name: "ymg_session",
        value: "not-a-valid.token!!!",
        domain: "127.0.0.1",
        path: "/",
        httpOnly: true,
        sameSite: "Lax",
        secure: false,
      },
    ]);
    const api = await page.request.get("/api/backend/health");
    expect(api.status()).toBe(401);
    await page.goto("/scheduler");
    await expect(page).toHaveURL(/\/login/);
  });

  test("誤ったパスワードは失敗する", async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-username").fill(ADMIN_USER);
    await page.getByTestId("login-password").fill("definitely-wrong-password");
    await page.getByTestId("login-submit").click();
    await expect(page.getByTestId("login-error")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("ログイン成功でセッション cookie が付く", async ({ page, context }) => {
    await loginViaUi(page);
    const cookies = await context.cookies();
    const session = cookies.find((c) => c.name === "ymg_session");
    expect(session).toBeTruthy();
    expect(session?.httpOnly).toBe(true);
    expect(session?.sameSite?.toLowerCase()).toBe("lax");
    expect(session?.path).toBe("/");
    // LAN HTTP: Secure=false
    expect(session?.secure).toBe(false);
    await expect(page.getByTestId("header-username")).toContainText(ADMIN_USER);
  });

  test("オープンリダイレクト next は拒否される", async ({ page }) => {
    await page.goto("/login?next=//evil.com");
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
    expect(page.url()).not.toContain("evil.com");
    await expect(page).toHaveURL(/\/($|\?)/);
  });

  test("ログアウト POST で cookie 破棄と /login 遷移", async ({ page, context }) => {
    await loginViaUi(page);
    await page.getByTestId("header-logout").click();
    await expect(page).toHaveURL(/\/login/);
    const cookies = await context.cookies();
    const session = cookies.find((c) => c.name === "ymg_session");
    expect(!session || session.value === "").toBeTruthy();
  });

  test("GET logout は許可しない", async ({ page }) => {
    await loginViaApi(page);
    const res = await page.request.get("/api/auth/logout");
    expect(res.status()).toBe(405);
  });

  test("保護 API はログイン後 401 以外(backend 応答依存)", async ({ page }) => {
    await loginViaApi(page);
    // backend 未起動でもセッション通過後は 502/200 のどちらか(401 ではない)
    const res = await page.request.get("/api/backend/health");
    expect(res.status()).not.toBe(401);
  });

  test("BFF unsafe メソッドのクロスオリジンは 403", async ({ page }) => {
    await loginViaApi(page);
    const res = await page.request.post("/api/backend/scheduler", {
      headers: {
        Origin: "http://evil.example",
        "Content-Type": "application/json",
      },
      data: { enabled: true },
    });
    expect(res.status()).toBe(403);
  });

  test("login POST のクロスオリジンは 403", async ({ page }) => {
    const res = await page.request.post("/api/auth/login", {
      headers: {
        Origin: "http://evil.example",
        "Content-Type": "application/json",
      },
      data: { username: "x", password: "y" },
    });
    expect(res.status()).toBe(403);
  });

  test("ブラウザ: ログイン → 管理画面 → ログアウト", async ({ page }) => {
    await loginViaUi(page);
    await expect(page.getByTestId("sidebar-username")).toContainText(ADMIN_USER);
    await page.goto("/llm");
    await expect(page).toHaveURL(/\/llm/);
    await page.getByTestId("sidebar-logout").click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/llm");
    await expect(page).toHaveURL(/\/login/);
  });
});
