import { test, expect, type Page, type Route } from "@playwright/test";

import { loginViaApi } from "./helpers";

// Plan 詳細の音声再生/ダウンロード(screen-spec.md §2 ③)。
// backend は page.route でモック。 実 GPU 生成は行わない。

const PLAN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const POST_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

async function mockPlanDetailApi(page: Page): Promise<void> {
  await page.route(/\/api\/backend\/plans(\/|\?|$)/, async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/plans/${PLAN_ID}`) && route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: PLAN_ID,
          cycle: "daily",
          target_date: "2026-07-11",
          payload: {},
          rationale: "視聴維持率を上げる日次計画",
          status: "music_generated",
          llm_provider: "ollama",
          llm_model: "qwen2.5:3b",
          llm_prompt_version: "v1",
          llm_cost_usd: 0,
          created_at: "2026-07-11T00:00:00+09:00",
          approved_at: "2026-07-11T01:00:00+09:00",
        }),
      });
      return;
    }
    await route.continue();
  });

  await page.route(/\/api\/backend\/posts(\/|\?|$)/, async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (method === "GET" && path.endsWith("/posts")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: POST_ID,
              plan_id: PLAN_ID,
              position: 0,
              genre: "lo-fi hip hop",
              status: "music_generated",
              final_title: "雨の Lo-Fi",
              payload: { mood: "calm", bpm_range: "70-90" },
              created_at: "2026-07-11T02:00:00+09:00",
            },
          ],
        }),
      });
      return;
    }

    if (method === "GET" && path.endsWith(`/posts/${POST_ID}/tracks`)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: Array.from({ length: 6 }, (_, position) => ({
            id: `track-${position}`,
            post_id: POST_ID,
            position,
            duration_sec: 60,
            bpm: 80 + position,
            subtheme: `theme-${position}`,
            acoustid_status: "not_checked",
            generated_at: "2026-07-11T02:10:00+09:00",
          })),
        }),
      });
      return;
    }

    if (
      method === "GET" &&
      path.includes(`/posts/${POST_ID}/tracks/`) &&
      path.endsWith("/audio")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "audio/wav",
        body: Buffer.from("RIFFxxxxWAVEfmt "),
      });
      return;
    }

    if (
      method === "GET" &&
      path.includes(`/posts/${POST_ID}/tracks/`) &&
      path.endsWith("/download")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "audio/wav",
        headers: {
          "content-disposition": 'attachment; filename="track-0.wav"',
        },
        body: Buffer.from("RIFFxxxxWAVEfmt "),
      });
      return;
    }

    await route.continue();
  });
}

test.describe("Plan 詳細 音声再生/ダウンロード", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
    await mockPlanDetailApi(page);
  });

  test("Post 別 6 トラックと再生ボタンが表示され、再生要求まで audio を取得しない", async ({
    page,
  }) => {
    let audioFetchCount = 0;
    page.on("request", (request) => {
      if (request.url().includes("/audio")) {
        audioFetchCount += 1;
      }
    });

    await page.goto(`/plans/${PLAN_ID}`);

    await expect(page.getByTestId("plan-detail")).toBeVisible();
    await expect(page.getByTestId("plan-detail-status")).toContainText(
      "音楽生成済",
    );
    await expect(page.getByTestId(`plan-post-${POST_ID}`)).toBeVisible();

    for (let position = 0; position < 6; position += 1) {
      await expect(
        page.getByTestId(`plan-track-${POST_ID}-${position}`),
      ).toHaveAttribute("data-generated", "true");
      await expect(
        page.getByTestId(`plan-track-play-${POST_ID}-${position}`),
      ).toBeVisible();
      await expect(
        page.getByTestId(`plan-track-audio-${POST_ID}-${position}`),
      ).toHaveCount(0);
    }

    expect(audioFetchCount).toBe(0);

    await page.getByTestId(`plan-track-play-${POST_ID}-0`).click();
    await expect(
      page.getByTestId(`plan-track-audio-${POST_ID}-0`),
    ).toBeVisible();
    expect(audioFetchCount).toBe(1);
  });

  test("ダウンロードボタンが有効", async ({ page }) => {
    await page.goto(`/plans/${PLAN_ID}`);
    const downloadBtn = page.getByTestId(`plan-track-download-${POST_ID}-0`);
    await expect(downloadBtn).toBeEnabled();
  });
});

test.describe("Plan 一覧の詳細リンク", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
  });

  test("詳細を見るで Plan 詳細へ遷移できる", async ({ page }) => {
    await page.route(/\/api\/backend\/(plans|posts)(\/|\?|$)/, async (route: Route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      const method = route.request().method();

      if (method === "GET" && /\/plans$/.test(path)) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [
              {
                id: PLAN_ID,
                cycle: "daily",
                target_date: "2026-07-11",
                payload: {},
                rationale: "test",
                status: "approved",
                llm_provider: "ollama",
                llm_model: "qwen2.5:3b",
                llm_prompt_version: "v1",
                llm_cost_usd: 0,
                created_at: "2026-07-11T00:00:00+09:00",
              },
            ],
            total: 1,
          }),
        });
        return;
      }

      if (method === "GET" && path.endsWith(`/plans/${PLAN_ID}`)) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: PLAN_ID,
            cycle: "daily",
            target_date: "2026-07-11",
            payload: {},
            rationale: "視聴維持率を上げる日次計画",
            status: "music_generated",
            llm_provider: "ollama",
            llm_model: "qwen2.5:3b",
            llm_prompt_version: "v1",
            llm_cost_usd: 0,
            created_at: "2026-07-11T00:00:00+09:00",
            approved_at: "2026-07-11T01:00:00+09:00",
          }),
        });
        return;
      }

      if (method === "GET" && /\/posts$/.test(path)) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items: [] }),
        });
        return;
      }

      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ message: "unmocked" }),
      });
    });

    await page.goto("/plans");
    await expect(page.getByTestId("plan-card")).toBeVisible();
    await page.getByTestId("plan-detail-link").click();
    await expect(page).toHaveURL(new RegExp(`/plans/${PLAN_ID}`));
    await expect(page.getByTestId("plan-detail")).toBeVisible();
  });
});
