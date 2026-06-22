import { test, expect, type Page, type Route } from "@playwright/test";

import { BASIC_AUTH } from "./helpers";

// US4 スケジューラ運用画面の critical user flow(screen-spec.md / ADR-0031)。
//  - scheduler 有効/無効トグル(GET /scheduler → PUT /scheduler → 楽観 refetch)
//  - 投稿モード切替(PUT /scheduler/mode の戻り値で楽観反映)
//  - コンプラ緊急停止(ダイアログ → window_hours バリデーション → POST /scheduler/panic-stop)
//
// data-testid 契約: scheduler-enabled-toggle / scheduler-mode-toggle / panic-stop-btn /
//   panic-stop-window-input / panic-stop-confirm-btn / panic-stop-cancel-btn / panic-stop-result
// backend はすべて page.route(正規表現)でモックする。

interface SchedulerState {
  enabled: boolean;
  updated_at: string;
}

/** scheduler 系 API をモックする。 state は閉包で保持し PUT で書き換える。 */
async function mockSchedulerApi(page: Page): Promise<void> {
  const state: SchedulerState = {
    enabled: false,
    updated_at: "2026-06-22T00:00:00+09:00",
  };

  await page.route(/\/api\/backend\/scheduler/, async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    // POST /scheduler/panic-stop
    if (method === "POST" && path.endsWith("/scheduler/panic-stop")) {
      state.enabled = false;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          scheduler_enabled: false,
          updated_count: 1,
          recent_videos: [
            {
              youtube_video_id: "vid-001",
              title: "lo-fi hip hop mix",
              posted_at: "2026-06-22T09:00:00+09:00",
              privacy_status: "private",
              contains_synthetic_media: true,
            },
          ],
        }),
      });
      return;
    }

    // PUT /scheduler/mode
    if (method === "PUT" && path.endsWith("/scheduler/mode")) {
      const body = request.postDataJSON() as { dryrun_enabled?: boolean };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ dryrun_enabled: Boolean(body?.dryrun_enabled) }),
      });
      return;
    }

    // GET/PUT /scheduler
    if (path.endsWith("/scheduler")) {
      if (method === "PUT") {
        const body = request.postDataJSON() as { enabled?: boolean };
        state.enabled = Boolean(body?.enabled);
        state.updated_at = "2026-06-22T01:00:00+09:00";
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(state),
      });
      return;
    }

    await route.continue();
  });
}

test.describe("[FR-073/FR-102] スケジューラ運用画面 (US4)", () => {
  test.use({ httpCredentials: BASIC_AUTH });

  test.beforeEach(async ({ page }) => {
    await mockSchedulerApi(page);
  });

  test("scheduler 有効/無効をトグルできる", async ({ page }) => {
    await page.goto("/scheduler");

    const toggle = page.getByTestId("scheduler-enabled-toggle");
    await expect(toggle).toHaveText("有効にする"); // 初期は無効
    await expect(page.getByText("無効", { exact: true })).toBeVisible();

    await toggle.click();

    // PUT 後に GET refetch され、 有効状態に更新される。
    await expect(toggle).toHaveText("無効にする");
    await expect(page.getByText("有効", { exact: true })).toBeVisible();
  });

  test("投稿モード(Dryrun↔本番)を切り替えられる", async ({ page }) => {
    await page.goto("/scheduler");

    const modeToggle = page.getByTestId("scheduler-mode-toggle");
    // dryrunEnabled 初期 null → nextDryrun=true → ボタンは「Dryrun に切替」。
    await expect(modeToggle).toHaveText("Dryrun に切替");

    await modeToggle.click();

    // 戻り値 {dryrun_enabled:true} で楽観反映 → バッジ Dryrun、 次は本番へ切替。
    await expect(modeToggle).toHaveText("本番投稿に切替");
    await expect(page.getByText("Dryrun", { exact: true })).toBeVisible();
  });

  test("緊急停止: window バリデーション → 実行で結果が表示される", async ({
    page,
  }) => {
    await page.goto("/scheduler");

    await page.getByTestId("panic-stop-btn").click();

    const windowInput = page.getByTestId("panic-stop-window-input");
    await expect(windowInput).toBeVisible();

    const confirm = page.getByTestId("panic-stop-confirm-btn");
    await expect(confirm).toBeEnabled(); // 既定 24 は有効

    // 0(1 未満)は無効化される。
    await windowInput.fill("0");
    await expect(confirm).toBeDisabled();

    await windowInput.fill("24");
    await expect(confirm).toBeEnabled();

    await confirm.click();

    const result = page.getByTestId("panic-stop-result");
    await expect(result).toBeVisible();
    await expect(result).toContainText("private 化した本数");
    await expect(result).toContainText("lo-fi hip hop mix");
  });

  test("緊急停止: キャンセルすると結果は出ない", async ({ page }) => {
    await page.goto("/scheduler");

    await page.getByTestId("panic-stop-btn").click();
    await expect(page.getByTestId("panic-stop-window-input")).toBeVisible();

    await page.getByTestId("panic-stop-cancel-btn").click();
    await expect(page.getByTestId("panic-stop-result")).toHaveCount(0);
  });
});
