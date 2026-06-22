import { test, expect, type Page, type Route } from "@playwright/test";

import { BASIC_AUTH } from "./helpers";

// US6 ジョブ進捗画面の critical user flow(SSE 可視化 / screen-spec.md)。
//  - /jobs/stream(text/event-stream)を購読し、 step×genre グリッドをリアルタイム更新
//  - data-testid 契約: jobs-grid / jobs-connection-status(data-connection) /
//    jobs-cell-{genre}-{step}(data-status) / 列見出し data-genre
//
// backend SSE を page.route でモックする。 Playwright の fulfill は有限ボディのため
// EOF 後にラッパが再接続するが、 セル状態は冪等で保持される(再接続しても succeeded のまま)。

const SSE_BODY = [
  // 全体列(genre なし)の cycle ステップ = 実行中。
  `data: ${JSON.stringify({
    timestamp: "2026-06-22T10:00:00+09:00",
    job_name: "daily_cycle",
    step: "cycle",
    status: "running",
  })}`,
  "",
  // ジャンル列(lo-fi hip hop)の music ステップ = 成功。
  `data: ${JSON.stringify({
    timestamp: "2026-06-22T10:00:01+09:00",
    job_name: "daily_cycle",
    step: "music",
    status: "succeeded",
    genre: "lo-fi hip hop",
  })}`,
  "",
  "",
].join("\n");

async function mockJobsStream(page: Page): Promise<void> {
  await page.route(/\/api\/backend\/jobs\/stream/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "cache-control": "no-cache" },
      body: SSE_BODY,
    });
  });
}

test.describe("ジョブ進捗画面 (US6, SSE)", () => {
  test.use({ httpCredentials: BASIC_AUTH });

  test.beforeEach(async ({ page }) => {
    await mockJobsStream(page);
  });

  test("グリッドが描画され、 全体列と step 行が並ぶ", async ({ page }) => {
    await page.goto("/jobs");

    await expect(page.getByTestId("jobs-grid")).toBeVisible();
    // 全体列ヘッダ(genre なし step 集約)。
    await expect(page.locator('[data-genre="__global__"]')).toBeVisible();
    // step 行見出し(固定 7 行)の一部。
    await expect(page.getByText("音楽", { exact: true })).toBeVisible();
    await expect(page.getByText("公開", { exact: true })).toBeVisible();
  });

  test("SSE 受信でジャンル列が増え、 セルが状態色で更新される", async ({
    page,
  }) => {
    await page.goto("/jobs");

    // ジャンル列が動的に追加される。
    await expect(page.locator('[data-genre="lo-fi hip hop"]')).toBeVisible();

    // 全体 cycle = 実行中、 ジャンル music = 成功。
    await expect(
      page.getByTestId("jobs-cell-__global__-cycle"),
    ).toHaveAttribute("data-status", "running");
    await expect(
      page.getByTestId("jobs-cell-lo-fi hip hop-music"),
    ).toHaveAttribute("data-status", "succeeded");
  });

  test("接続状態バッジが connecting から進行する(接続成立)", async ({
    page,
  }) => {
    await page.goto("/jobs");

    const badge = page.getByTestId("jobs-connection-status");
    await expect(badge).toBeVisible();

    // 接続成立後は connected(瞬間)→ EOF で reconnecting(バックオフ中は安定)。
    // 初期 connecting から進行したこと = SSE 接続が成立したことを示す。
    await expect
      .poll(() => badge.getAttribute("data-connection"), { timeout: 15_000 })
      .not.toBe("connecting");
  });
});
