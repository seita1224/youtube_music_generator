import { test, expect, type Page, type Route } from "@playwright/test";

import { BASIC_AUTH } from "./helpers";

// 画面10 ジャンル管理の critical user flow(US3 / FR-037・FR-038)。
//  - ジャンル辞書を role 別に一覧(GET /genres)
//  - experiment ジャンルの採用(promote: role 1 段昇格 + enabled)/ 削除(disable)を承認
//
// data-testid 契約: genres-table / genre-row-{name} / genre-role-{name} /
//   genre-promote-{name} / genre-disable-{name} / genres-action-error
// backend は page.route(正規表現)でモックする。

interface MockGenre {
  name: string;
  display_name: string;
  role: string;
  enabled: boolean;
  bpm_min: number;
  bpm_max: number;
  description: string;
  created_at: string;
  updated_at: string;
}

function seedGenres(): MockGenre[] {
  const ts = "2026-06-01T00:00:00+09:00";
  return [
    { name: "lo-fi-hip-hop", display_name: "Lo-Fi Hip Hop", role: "primary", enabled: true, bpm_min: 70, bpm_max: 90, description: "", created_at: ts, updated_at: ts },
    { name: "future-garage", display_name: "Future Garage", role: "experimental", enabled: true, bpm_min: 130, bpm_max: 140, description: "", created_at: ts, updated_at: ts },
    { name: "vaporwave", display_name: "Vaporwave", role: "extended", enabled: true, bpm_min: 60, bpm_max: 80, description: "", created_at: ts, updated_at: ts },
  ];
}

const _LADDER = ["experimental", "extended", "primary"];

/** genres 系 API をモックする。 state は閉包で保持し promote/disable で書き換える。 */
async function mockGenresApi(page: Page): Promise<void> {
  const genres = seedGenres();
  const find = (path: string): MockGenre | undefined => {
    // /api/backend/genres/{name}/promote(or disable) から name を取り出す。
    const parts = path.split("/");
    const name = parts[parts.length - 2];
    return genres.find((g) => g.name === name);
  };

  await page.route(/\/api\/backend\/genres/, async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (method === "POST" && path.endsWith("/promote")) {
      const genre = find(path);
      if (!genre) {
        await route.fulfill({ status: 404, body: "{}" });
        return;
      }
      const idx = _LADDER.indexOf(genre.role);
      genre.role = _LADDER[Math.min(idx + 1, _LADDER.length - 1)];
      genre.enabled = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(genre),
      });
      return;
    }

    if (method === "POST" && path.endsWith("/disable")) {
      const genre = find(path);
      if (!genre) {
        await route.fulfill({ status: 404, body: "{}" });
        return;
      }
      genre.enabled = false;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(genre),
      });
      return;
    }

    if (method === "GET" && path.endsWith("/genres")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: genres, total: genres.length }),
      });
      return;
    }

    await route.continue();
  });
}

test.describe("[FR-037/FR-038] ジャンル管理画面 (US3)", () => {
  test.use({ httpCredentials: BASIC_AUTH });

  test.beforeEach(async ({ page }) => {
    await mockGenresApi(page);
  });

  test("ジャンルが role 別に一覧表示される", async ({ page }) => {
    await page.goto("/genres");

    await expect(page.getByTestId("genres-table")).toBeVisible();
    await expect(page.getByTestId("genre-row-lo-fi-hip-hop")).toBeVisible();
    await expect(page.getByTestId("genre-role-lo-fi-hip-hop")).toHaveText("主力");
    await expect(page.getByTestId("genre-role-future-garage")).toHaveText("実験");
  });

  test("実験ジャンルを昇格(採用)すると role が 1 段上がる", async ({ page }) => {
    await page.goto("/genres");

    await expect(page.getByTestId("genre-role-future-garage")).toHaveText("実験");
    await page.getByTestId("genre-promote-future-garage").click();

    // promote → refetch で experimental → extended に反映。
    await expect(page.getByTestId("genre-role-future-garage")).toHaveText("拡張");
  });

  test("主力ジャンルの昇格ボタンは無効(最上位)", async ({ page }) => {
    await page.goto("/genres");

    await expect(page.getByTestId("genre-promote-lo-fi-hip-hop")).toBeDisabled();
  });

  test("ジャンルを無効化すると状態が無効になる", async ({ page }) => {
    await page.goto("/genres");

    const row = page.getByTestId("genre-row-vaporwave");
    await expect(row).toContainText("有効");

    await page.getByTestId("genre-disable-vaporwave").click();

    await expect(row).toContainText("無効");
    // 無効化後は昇格で再有効化できるよう、 無効化ボタンは無効になる。
    await expect(page.getByTestId("genre-disable-vaporwave")).toBeDisabled();
  });
});
