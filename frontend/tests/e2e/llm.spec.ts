import { test, expect, type Page, type Route } from "@playwright/test";

import { BASIC_AUTH } from "./helpers";

// US5 LLM 設定画面の critical user flow(ADR-0019 / ADR-0024)。
//  - provider/auth_mode/model の表示・切替(GET /llm/providers)
//  - 保存(PUT /llm/providers)成功表示
//  - 不正組合せ(FR-022 相当)で 400 → llm-save-error 表示
//  - 月次コスト/予算進捗バー(GET /llm/usage)
//
// data-testid 契約: llm-provider-select / llm-authmode-select / llm-model-select /
//   llm-save-btn / llm-save-error / llm-budget-pct / llm-budget-bar / llm-usage-table

const PROVIDERS = [
  {
    provider: "openai",
    available: true,
    // codex_oauth を併記し、 openai+codex_oauth を「不正組合せ」として 400 を返す(FR-022 模擬)。
    auth_modes: ["api_key", "codex_oauth"],
    models: ["gpt-4o-mini"],
  },
  {
    provider: "anthropic",
    available: true,
    auth_modes: ["api_key"],
    models: ["claude-3-5-sonnet"],
  },
  {
    provider: "ollama",
    available: false,
    auth_modes: ["api_key"],
    models: ["llama3.1"],
  },
];

const USAGE = {
  month: "2026-06",
  total_cost_usd: 8.5,
  budget_usd: 10,
  budget_pct: 85, // 80% 以上 → danger 色
  by_provider: {
    openai: {
      cost_usd: 8.5,
      prompt_tokens: 1000,
      cached_tokens: 200,
      completion_tokens: 500,
    },
  },
};

/** llm 系 API をモックする。 openai+codex_oauth の PUT のみ 400 を返す。 */
async function mockLlmApi(page: Page): Promise<void> {
  await page.route(/\/api\/backend\/llm/, async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path.includes("/llm/usage")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(USAGE),
      });
      return;
    }

    if (path.endsWith("/llm/providers")) {
      if (method === "PUT") {
        const body = request.postDataJSON() as {
          provider?: string;
          auth_mode?: string;
        };
        if (body?.provider === "openai" && body?.auth_mode === "codex_oauth") {
          await route.fulfill({
            status: 400,
            contentType: "application/json",
            body: JSON.stringify({
              detail: "codex_oauth は openai では使用できません。",
            }),
          });
          return;
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "{}",
        });
        return;
      }
      // GET
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PROVIDERS),
      });
      return;
    }

    await route.continue();
  });
}

test.describe("[FR-020/FR-022] LLM 設定画面 (US5)", () => {
  test.use({ httpCredentials: BASIC_AUTH });

  test.beforeEach(async ({ page }) => {
    await mockLlmApi(page);
  });

  test("provider/auth_mode/model が表示され既定選択される", async ({ page }) => {
    await page.goto("/llm");

    await expect(page.getByTestId("llm-provider-select")).toHaveValue("openai");
    await expect(page.getByTestId("llm-authmode-select")).toHaveValue("api_key");
    await expect(page.getByTestId("llm-model-select")).toHaveValue("gpt-4o-mini");
  });

  test("有効な組合せを保存すると成功表示が出る", async ({ page }) => {
    await page.goto("/llm");

    await page.getByTestId("llm-provider-select").selectOption("anthropic");
    await expect(page.getByTestId("llm-authmode-select")).toHaveValue("api_key");

    await page.getByTestId("llm-save-btn").click();
    await expect(page.getByText("保存しました。")).toBeVisible();
    await expect(page.getByTestId("llm-save-error")).toHaveCount(0);
  });

  test("不正組合せ(openai+codex_oauth)は 400 でエラー表示", async ({ page }) => {
    await page.goto("/llm");

    await page.getByTestId("llm-provider-select").selectOption("openai");
    await page.getByTestId("llm-authmode-select").selectOption("codex_oauth");

    await page.getByTestId("llm-save-btn").click();

    const error = page.getByTestId("llm-save-error");
    await expect(error).toBeVisible();
    await expect(error).toContainText("codex_oauth");
  });

  test("月次コスト/予算進捗バーが表示される", async ({ page }) => {
    await page.goto("/llm");

    await expect(page.getByTestId("llm-budget-pct")).toHaveText("85.0%");

    const bar = page.getByTestId("llm-budget-bar");
    await expect(bar).toBeVisible();
    await expect(bar).toHaveAttribute("aria-valuenow", "85");

    await expect(page.getByTestId("llm-usage-table")).toBeVisible();
    await expect(page.getByTestId("llm-usage-table")).toContainText("openai");
  });
});
