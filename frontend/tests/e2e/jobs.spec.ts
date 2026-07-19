import { test, expect, type Page, type Route } from "@playwright/test";

import { loginViaApi } from "./helpers";

// ジョブ進捗画面(screen-spec.md §2 ⑧)。
//  - GET /jobs/runs で実行履歴
//  - URL ?run_id= で対象復元 + REST DB snapshot の工程タイムライン
//  - running のときだけ SSE /jobs/stream?run_id=...(初期再送は除外)

const RUN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const PLAN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const RUN_ID_2 = "cccccccc-cccc-cccc-cccc-cccccccccccc";

const RUNS_BODY = {
  items: [
    {
      run_id: RUN_ID,
      job_name: "music_generation",
      status: "running",
      trigger: "run_now",
      target_date: "2026-07-11",
      plan_id: PLAN_ID,
      started_at: "2026-07-11T10:00:00+09:00",
      finished_at: null,
      duration_ms: null,
    },
    {
      run_id: RUN_ID_2,
      job_name: "music_generation",
      status: "succeeded",
      trigger: "cron",
      target_date: "2026-07-10",
      plan_id: PLAN_ID,
      started_at: "2026-07-10T07:00:00+09:00",
      finished_at: "2026-07-10T07:05:00+09:00",
      duration_ms: 300_000,
    },
  ],
};

const EVENTS_BODY = {
  run_id: RUN_ID,
  items: [
    {
      id: "e1",
      run_id: RUN_ID,
      step: "cycle",
      status: "succeeded",
      created_at: "2026-07-11T10:00:01+09:00",
    },
    {
      id: "e2",
      run_id: RUN_ID,
      step: "music",
      status: "running",
      genre: "lo-fi hip hop",
      created_at: "2026-07-11T10:00:02+09:00",
    },
  ],
};

/** SSE は REST snapshot と同じ 2 件 + ライブ 1 件を送る(重複除外の検証用)。 */
const SSE_BODY = [
  `data: ${JSON.stringify({
    timestamp: "2026-07-11T10:00:01+09:00",
    run_id: RUN_ID,
    job_name: "music_generation",
    step: "cycle",
    status: "succeeded",
  })}`,
  "",
  `data: ${JSON.stringify({
    timestamp: "2026-07-11T10:00:02+09:00",
    run_id: RUN_ID,
    job_name: "music_generation",
    step: "music",
    status: "running",
    genre: "lo-fi hip hop",
  })}`,
  "",
  `data: ${JSON.stringify({
    timestamp: "2026-07-11T10:00:03+09:00",
    run_id: RUN_ID,
    job_name: "music_generation",
    step: "music",
    status: "succeeded",
    genre: "lo-fi hip hop",
  })}`,
  "",
  "",
].join("\n");

async function mockJobsApi(page: Page): Promise<void> {
  await page.route(/\/api\/backend\/jobs\//, async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path.endsWith("/jobs/runs") && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(RUNS_BODY),
      });
      return;
    }

    const eventsMatch = path.match(/\/jobs\/runs\/([^/]+)\/events$/);
    if (eventsMatch && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...EVENTS_BODY,
          run_id: eventsMatch[1],
        }),
      });
      return;
    }

    if (path.endsWith("/jobs/stream") && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "cache-control": "no-cache" },
        body: SSE_BODY,
      });
      return;
    }

    await route.continue();
  });
}

test.describe("ジョブ進捗画面 (履歴 + SSE)", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
    await mockJobsApi(page);
  });

  test("実行履歴と DB snapshot の工程が表示される", async ({ page }) => {
    await page.goto(`/jobs?run_id=${RUN_ID}`);

    await expect(page.getByTestId("jobs-run-list")).toBeVisible();
    await expect(page.getByTestId(`jobs-run-row-${RUN_ID}`)).toHaveAttribute(
      "data-selected",
      "true",
    );
    await expect(page.getByTestId("jobs-timeline")).toBeVisible();
    await expect(page.getByTestId("jobs-step-cycle")).toHaveAttribute(
      "data-status",
      "succeeded",
    );
    await expect(page.getByTestId("jobs-plan-link")).toBeVisible();
  });

  test("run_id 省略時は直近実行を選択する", async ({ page }) => {
    await page.goto("/jobs");

    await expect(page.getByTestId(`jobs-run-row-${RUN_ID}`)).toHaveAttribute(
      "data-selected",
      "true",
    );
  });

  test("履歴行クリックで run_id を切替える", async ({ page }) => {
    await page.goto(`/jobs?run_id=${RUN_ID}`);
    await page.getByTestId(`jobs-run-row-${RUN_ID_2}`).click();
    await expect(page).toHaveURL(new RegExp(`run_id=${RUN_ID_2}`));
  });

  test("running のとき SSE 接続状態が進む", async ({ page }) => {
    await page.goto(`/jobs?run_id=${RUN_ID}`);

    const badge = page.getByTestId("jobs-connection-status");
    await expect
      .poll(() => badge.getAttribute("data-connection"), { timeout: 15_000 })
      .not.toBe("connecting");
  });

  test("SSE 初期再送は重複表示せずライブ差分だけ反映する", async ({ page }) => {
    await page.goto(`/jobs?run_id=${RUN_ID}`);

    // REST snapshot: cycle + music(running)。 SSE 再送分は除外し、 ライブの music(succeeded) のみ追加 → 3 行。
    await expect
      .poll(
        async () => page.locator('[data-testid="jobs-timeline"] li').count(),
        { timeout: 15_000 },
      )
      .toBe(3);
    await expect(page.getByTestId("jobs-step-cycle")).toHaveCount(1);
    await expect(page.getByTestId("jobs-step-music")).toHaveCount(2);
    await expect(
      page.locator('[data-testid="jobs-step-music"][data-status="succeeded"]'),
    ).toHaveCount(1);
  });
});

test.describe("ジョブ進捗画面 (終端 refetch)", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
  });

  test("running → succeeded で永続 events を再取得する", async ({ page }) => {
    let runStatus: "running" | "succeeded" = "running";
    let eventsFetchCount = 0;

    await page.route(/\/api\/backend\/jobs\//, async (route: Route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;

      if (path.endsWith("/jobs/runs") && request.method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [
              {
                run_id: RUN_ID,
                job_name: "music_generation",
                status: runStatus,
                trigger: "run_now",
                target_date: "2026-07-11",
                plan_id: PLAN_ID,
                started_at: "2026-07-11T10:00:00+09:00",
                finished_at:
                  runStatus === "succeeded"
                    ? "2026-07-11T10:05:00+09:00"
                    : null,
                duration_ms: runStatus === "succeeded" ? 300_000 : null,
              },
            ],
          }),
        });
        return;
      }

      const eventsMatch = path.match(/\/jobs\/runs\/([^/]+)\/events$/);
      if (eventsMatch && request.method() === "GET") {
        eventsFetchCount += 1;
        const items =
          runStatus === "running"
            ? EVENTS_BODY.items
            : [
                ...EVENTS_BODY.items,
                {
                  id: "e3",
                  run_id: RUN_ID,
                  step: "post",
                  status: "succeeded",
                  created_at: "2026-07-11T10:00:04+09:00",
                },
              ];
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ run_id: RUN_ID, items }),
        });
        return;
      }

      if (path.endsWith("/jobs/stream") && request.method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          headers: { "cache-control": "no-cache" },
          body: ": ping\n\n",
        });
        return;
      }

      await route.continue();
    });

    await page.goto(`/jobs?run_id=${RUN_ID}`);
    await expect(page.getByTestId("jobs-step-music")).toHaveAttribute(
      "data-status",
      "running",
    );
    const initialEventsFetches = eventsFetchCount;
    expect(initialEventsFetches).toBeGreaterThanOrEqual(1);

    // 次の runs refetchInterval(15s) で succeeded を返し、 終端遷移で events を invalidate する。
    runStatus = "succeeded";

    await expect
      .poll(() => eventsFetchCount, { timeout: 25_000 })
      .toBeGreaterThan(initialEventsFetches);

    await expect(page.getByTestId("jobs-step-post")).toHaveAttribute(
      "data-status",
      "succeeded",
    );
  });
});
