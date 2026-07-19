import { test, expect, type Page, type Route } from "@playwright/test";

import { loginViaApi } from "./helpers";

test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
});

// US4 スケジューラ運用画面の critical user flow(screen-spec.md / ADR-0031)。
//  - scheduler 有効/無効トグル(GET /scheduler → PUT /scheduler → 楽観 refetch)
//  - 投稿モード切替(PUT /scheduler/mode の戻り値で楽観反映)
//  - コンプラ緊急停止(ダイアログ → window_hours バリデーション → POST /scheduler/panic-stop)
//  - 承認済み Plan 選択 → run-now → /jobs?run_id= 遷移 / 409 UX
//
// data-testid 契約: scheduler-enabled-toggle / scheduler-mode-toggle / panic-stop-btn /
//   panic-stop-window-input / panic-stop-confirm-btn / panic-stop-cancel-btn / panic-stop-result
//   scheduler-run-now-plan / scheduler-run-now-btn / scheduler-run-now-error
// backend はすべて page.route(正規表現)でモックする。

interface SchedulerState {
  enabled: boolean;
  updated_at: string;
}

const APPROVED_PLAN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const RUN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

/** scheduler 系 API をモックする。 state は閉包で保持し PUT で書き換える。 */
async function mockSchedulerApi(
  page: Page,
  options?: {
    readonly approvedPlans?: boolean;
    readonly runNowStatus?: number;
    readonly runNowDetail?: string;
  },
): Promise<void> {
  const state: SchedulerState = {
    enabled: false,
    updated_at: "2026-06-22T00:00:00+09:00",
  };
  const includeApproved = options?.approvedPlans !== false;
  const runNowStatus = options?.runNowStatus ?? 202;
  const runNowDetail = options?.runNowDetail ?? "別の音楽生成が実行中";

  await page.route(/\/api\/backend\/plans(\/|\?|$)/, async (route: Route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    const items = includeApproved
      ? [
          {
            id: APPROVED_PLAN_ID,
            cycle: "daily",
            target_date: "2026-07-11",
            payload: { genre_distribution: { "lo-fi hip hop": 0.6 } },
            rationale: "test",
            status: "approved",
            llm_provider: "ollama",
            llm_model: "qwen2.5:3b",
            llm_prompt_version: "v1",
            llm_cost_usd: 0,
            created_at: "2026-07-11T00:00:00+09:00",
            approved_at: "2026-07-11T01:00:00+09:00",
          },
        ]
      : [];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items, total: items.length }),
    });
  });

  await page.route(/\/api\/backend\/scheduler/, async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (method === "POST" && path.endsWith("/scheduler/run-now")) {
      const body = request.postDataJSON() as { plan_id?: string };
      if (runNowStatus === 409) {
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ detail: runNowDetail }),
        });
        return;
      }
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          accepted: true,
          run_id: RUN_ID,
          plan_id: body?.plan_id ?? APPROVED_PLAN_ID,
          target_date: "2026-07-11",
        }),
      });
      return;
    }

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

    if (method === "PUT" && path.endsWith("/scheduler/mode")) {
      const body = request.postDataJSON() as { dryrun_enabled?: boolean };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ dryrun_enabled: Boolean(body?.dryrun_enabled) }),
      });
      return;
    }

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
test("scheduler 有効/無効をトグルできる", async ({ page }) => {
    await mockSchedulerApi(page);
    await page.goto("/scheduler");

    const toggle = page.getByTestId("scheduler-enabled-toggle");
    await expect(toggle).toHaveText("有効にする");
    await expect(page.getByText("無効", { exact: true })).toBeVisible();

    await toggle.click();

    await expect(toggle).toHaveText("無効にする");
    await expect(page.getByText("有効", { exact: true })).toBeVisible();
  });

  test("投稿モード(Dryrun↔本番)を切り替えられる", async ({ page }) => {
    await mockSchedulerApi(page);
    await page.goto("/scheduler");

    const modeToggle = page.getByTestId("scheduler-mode-toggle");
    await expect(modeToggle).toHaveText("Dryrun に切替");

    await modeToggle.click();

    await expect(modeToggle).toHaveText("本番投稿に切替");
    await expect(page.getByText("Dryrun", { exact: true })).toBeVisible();
  });

  test("緊急停止: window バリデーション → 実行で結果が表示される", async ({
    page,
  }) => {
    await mockSchedulerApi(page);
    await page.goto("/scheduler");

    await page.getByTestId("panic-stop-btn").click();

    const windowInput = page.getByTestId("panic-stop-window-input");
    await expect(windowInput).toBeVisible();

    const confirm = page.getByTestId("panic-stop-confirm-btn");
    await expect(confirm).toBeEnabled();

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
    await mockSchedulerApi(page);
    await page.goto("/scheduler");

    await page.getByTestId("panic-stop-btn").click();
    await expect(page.getByTestId("panic-stop-window-input")).toBeVisible();

    await page.getByTestId("panic-stop-cancel-btn").click();
    await expect(page.getByTestId("panic-stop-result")).toHaveCount(0);
  });

  test("承認済み Plan を選んで今すぐ生成すると /jobs?run_id= へ遷移する", async ({
    page,
  }) => {
    await mockSchedulerApi(page, { approvedPlans: true });
    await page.route(/\/api\/backend\/jobs\//, async (route: Route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/jobs/runs")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [
              {
                run_id: RUN_ID,
                job_name: "music_generation",
                status: "running",
                trigger: "run_now",
                target_date: "2026-07-11",
                plan_id: APPROVED_PLAN_ID,
                started_at: "2026-07-11T10:00:00+09:00",
              },
            ],
          }),
        });
        return;
      }
      if (path.includes("/events")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ run_id: RUN_ID, items: [] }),
        });
        return;
      }
      if (path.endsWith("/jobs/stream")) {
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: "\n\n",
        });
        return;
      }
      await route.continue();
    });

    await page.goto("/scheduler");

    await expect(page.getByTestId("scheduler-run-now-plan")).toBeVisible();
    await expect(page.getByTestId("scheduler-run-now-btn")).toBeEnabled();
    await page.getByTestId("scheduler-run-now-btn").click();

    await expect(page).toHaveURL(new RegExp(`/jobs\\?run_id=${RUN_ID}`));
  });

  test("実行中 409 のとき別の音楽生成が実行中と表示する", async ({ page }) => {
    await mockSchedulerApi(page, {
      approvedPlans: true,
      runNowStatus: 409,
      runNowDetail: "別の音楽生成が実行中",
    });
    await page.goto("/scheduler");

    await page.getByTestId("scheduler-run-now-btn").click();
    await expect(page.getByTestId("scheduler-run-now-error")).toContainText(
      "別の音楽生成が実行中",
    );
  });

  test("未承認 409 のとき承認済みではないと表示する", async ({ page }) => {
    await mockSchedulerApi(page, {
      approvedPlans: true,
      runNowStatus: 409,
      runNowDetail: "Plan is not approved (status=generated)",
    });
    await page.goto("/scheduler");

    await page.getByTestId("scheduler-run-now-btn").click();
    await expect(page.getByTestId("scheduler-run-now-error")).toContainText(
      "承認済みではありません",
    );
  });

  test("承認済み Plan が無いときボタン無効 + /plans 誘導", async ({ page }) => {
    await mockSchedulerApi(page, { approvedPlans: false });
    await page.goto("/scheduler");

    await expect(page.getByTestId("scheduler-run-now-btn")).toBeDisabled();
    await expect(page.getByTestId("scheduler-run-now-empty")).toBeVisible();
    await expect(
      page.getByTestId("scheduler-run-now-plans-link"),
    ).toHaveAttribute("href", "/plans");
  });
});
