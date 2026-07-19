import { test, expect, type Page, type Route } from "@playwright/test";

import { loginViaApi } from "./helpers";

// US5 LLM 設定画面: active 表示・model 永続化・env credential 非表示・Codex 未対応。
// data-testid: llm-active-* / llm-provider-select / llm-authmode-select /
//   llm-model-select / llm-save-btn / llm-save-error / llm-env-credential-notice /
//   llm-api-key-input / llm-codex-unsupported / llm-budget-*

const SETTINGS = {
  active: {
    provider: "ollama",
    auth_mode: "api_key",
    model: "qwen2.5:3b",
  },
  providers: [
    {
      provider: "openai",
      available: false,
      auth_modes: ["api_key", "codex_oauth"],
      models: ["gpt-4.1", "gpt-4.1-mini"],
      credential_source: "none",
      credential_configured: false,
      unsupported_auth_modes: ["codex_oauth"],
    },
    {
      provider: "anthropic",
      available: true,
      auth_modes: ["api_key"],
      models: ["claude-sonnet-4-6"],
      credential_source: "env",
      credential_configured: true,
      unsupported_auth_modes: [],
    },
    {
      provider: "ollama",
      available: true,
      auth_modes: ["api_key"],
      models: ["qwen2.5:3b", "llama3.2:3b", "gemma2:2b"],
      credential_source: "n/a",
      credential_configured: true,
      unsupported_auth_modes: [],
    },
  ],
};

const USAGE = {
  month: "2026-06",
  total_cost_usd: 8.5,
  budget_usd: 10,
  budget_pct: 85,
  by_provider: {
    openai: {
      cost_usd: 8.5,
      prompt_tokens: 1000,
      cached_tokens: 200,
      completion_tokens: 500,
    },
  },
};

let currentSettings = structuredClone(SETTINGS);

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

    if (path.includes("/llm/credentials")) {
      if (method === "PUT" || method === "DELETE") {
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "環境変数で設定済み（環境変数を使用）。",
          }),
        });
        return;
      }
    }

    if (path.endsWith("/llm/providers")) {
      if (method === "PUT") {
        const body = request.postDataJSON() as {
          provider?: string;
          auth_mode?: string;
          model?: string;
        };
        if (body?.auth_mode === "codex_oauth") {
          await route.fulfill({
            status: 422,
            contentType: "application/json",
            body: JSON.stringify({
              detail: "Codex OAuth (auth_mode='codex_oauth') は未実装のため保存できません。",
            }),
          });
          return;
        }
        if (
          body?.provider === "ollama" &&
          body.model &&
          !["qwen2.5:3b", "llama3.2:3b", "gemma2:2b"].includes(body.model)
        ) {
          await route.fulfill({
            status: 422,
            contentType: "application/json",
            body: JSON.stringify({ detail: "invalid model" }),
          });
          return;
        }
        currentSettings = {
          ...currentSettings,
          active: {
            provider: (body.provider ?? "ollama") as "ollama",
            auth_mode: (body.auth_mode ?? "api_key") as "api_key",
            model: body.model ?? currentSettings.active.model,
          },
        };
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(currentSettings.active),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(currentSettings),
      });
      return;
    }

    await route.continue();
  });
}

test.describe("[FR-020/FR-022] LLM 設定画面 (US5)", () => {
  test.beforeEach(async ({ page }) => {
    currentSettings = structuredClone(SETTINGS);
    await loginViaApi(page);
    await mockLlmApi(page);
  });

  test("active が Ollama のとき初期選択も Ollama", async ({ page }) => {
    await page.goto("/llm");

    await expect(page.getByTestId("llm-active-provider")).toContainText("Ollama");
    await expect(page.getByTestId("llm-active-model")).toHaveText("qwen2.5:3b");
    await expect(page.getByTestId("llm-provider-select")).toHaveValue("ollama");
    await expect(page.getByTestId("llm-model-select")).toHaveValue("qwen2.5:3b");
  });

  test("provider/auth/model native select の dark 配色と option contrast", async ({
    page,
  }) => {
    await page.goto("/llm");

    const providerSelect = page.getByTestId("llm-provider-select");
    const providerColors = await providerSelect.evaluate((element) => {
      const select = element as HTMLSelectElement;
      const openai = select.querySelector<HTMLOptionElement>(
        'option[value="openai"]',
      );
      const ollama = select.querySelector<HTMLOptionElement>(
        'option[value="ollama"]',
      );
      if (!openai || !ollama) {
        throw new Error("provider options are missing");
      }
      return {
        select: {
          background: getComputedStyle(select).backgroundColor,
          color: getComputedStyle(select).color,
          colorScheme: getComputedStyle(select).colorScheme,
        },
        unavailable: {
          background: getComputedStyle(openai).backgroundColor,
          color: getComputedStyle(openai).color,
          disabled: openai.disabled,
          muted: openai.dataset.muted,
        },
        current: {
          background: getComputedStyle(ollama).backgroundColor,
          color: getComputedStyle(ollama).color,
          text: ollama.textContent,
        },
      };
    });

    expect(providerColors.select).toEqual({
      background: "rgb(15, 23, 42)",
      color: "rgb(241, 245, 249)",
      colorScheme: "dark",
    });
    expect(providerColors.unavailable).toEqual({
      background: "rgb(30, 41, 59)",
      color: "rgb(148, 163, 184)",
      disabled: false,
      muted: "true",
    });
    expect(providerColors.current).toEqual({
      background: "rgb(15, 23, 42)",
      color: "rgb(241, 245, 249)",
      text: "Ollama（現在）",
    });

    await providerSelect.selectOption("openai");
    const disabledAuthColors = await page
      .getByTestId("llm-authmode-select")
      .locator('option[value="codex_oauth"]')
      .evaluate((option) => ({
        background: getComputedStyle(option).backgroundColor,
        color: getComputedStyle(option).color,
        disabled: (option as HTMLOptionElement).disabled,
      }));
    expect(disabledAuthColors).toEqual({
      background: "rgb(30, 41, 59)",
      color: "rgb(148, 163, 184)",
      disabled: true,
    });

    for (const testId of ["llm-authmode-select", "llm-model-select"]) {
      const colors = await page.getByTestId(testId).evaluate((select) => ({
        background: getComputedStyle(select).backgroundColor,
        color: getComputedStyle(select).color,
        colorScheme: getComputedStyle(select).colorScheme,
        enabledOptionColors: Array.from(
          (select as HTMLSelectElement).options,
        )
          .filter((option) => !option.disabled)
          .map((option) => ({
            background: getComputedStyle(option).backgroundColor,
            color: getComputedStyle(option).color,
          })),
      }));
      expect(colors.background).toBe("rgb(15, 23, 42)");
      expect(colors.color).toBe("rgb(241, 245, 249)");
      expect(colors.colorScheme).toBe("dark");
      expect(colors.enabledOptionColors.length).toBeGreaterThan(0);
      expect(
        colors.enabledOptionColors.every(
          (option) =>
            option.background === "rgb(15, 23, 42)" &&
            option.color === "rgb(241, 245, 249)",
        ),
      ).toBe(true);
    }
  });

  test("不整合 active を保持し、有効モデル選択後に修復保存できる", async ({
    page,
  }) => {
    currentSettings = {
      ...structuredClone(SETTINGS),
      active: {
        provider: "ollama",
        auth_mode: "api_key",
        model: "gpt-4.1",
      },
    };
    await page.goto("/llm");

    await expect(page.getByTestId("llm-active-model")).toHaveText("gpt-4.1");
    await expect(page.getByTestId("llm-model-select")).toHaveValue("gpt-4.1");
    await expect(page.getByTestId("llm-model-mismatch-notice")).toBeVisible();
    await expect(page.getByTestId("llm-save-btn")).toBeDisabled();

    await page.getByTestId("llm-model-select").selectOption("qwen2.5:3b");
    await expect(page.getByTestId("llm-model-mismatch-notice")).toHaveCount(0);
    await page.getByTestId("llm-save-btn").click();
    await expect(page.getByText("保存しました。")).toBeVisible();
    await expect(page.getByTestId("llm-active-model")).toHaveText("qwen2.5:3b");
  });

  test("モデル変更を保存すると成功表示が出る", async ({ page }) => {
    await page.goto("/llm");

    await page.getByTestId("llm-model-select").selectOption("llama3.2:3b");
    await page.getByTestId("llm-save-btn").click();
    await expect(page.getByText("保存しました。")).toBeVisible();
  });

  test("Codex OAuth は未対応表示で保存不可", async ({ page }) => {
    await page.goto("/llm");

    await page.getByTestId("llm-provider-select").selectOption("openai");
    // disabled option は selectOption できない場合があるので、 UI 文言を確認
    await expect(page.getByTestId("llm-authmode-select").locator("option[value=codex_oauth]")).toBeDisabled();
  });

  test("env credential 時は API キー入力を出さず案内を表示", async ({ page }) => {
    await page.goto("/llm");

    await page.getByTestId("llm-provider-select").selectOption("anthropic");
    await expect(page.getByTestId("llm-env-credential-notice")).toContainText(
      "環境変数で設定済み",
    );
    await expect(page.getByTestId("llm-api-key-input")).toHaveCount(0);
  });

  test("credential_source=none の openai では API キー入力が出る", async ({
    page,
  }) => {
    await page.goto("/llm");

    await page.getByTestId("llm-provider-select").selectOption("openai");
    await expect(page.getByTestId("llm-api-key-input")).toBeVisible();
  });

  test("月次コスト/予算進捗バーが表示される", async ({ page }) => {
    await page.goto("/llm");

    await expect(page.getByTestId("llm-budget-pct")).toHaveText("85.0%");
    const bar = page.getByTestId("llm-budget-bar");
    await expect(bar).toBeVisible();
    await expect(bar).toHaveAttribute("aria-valuenow", "85");
    await expect(page.getByTestId("llm-usage-table")).toContainText("openai");
  });
});
