import { test, expect, type Page, type Route } from "@playwright/test";

import { loginViaApi } from "./helpers";

// 画面10 ジャンル管理の critical user flow(US3 / FR-037・FR-038)。
//  - ジャンル辞書を role 別に一覧(GET /genres)
//  - 採用(promote: 1 段昇格 + enabled)/ 降格(demote: 1 段降格)/ 削除(disable)/ 手動追加(create)
//
// role 語彙は spec 準拠の experiment / extension / main。
// data-testid 契約: genres-table / genre-row-{name} / genre-role-{name} /
//   genre-promote-{name} / genre-demote-{name} / genre-disable-{name} /
//   genre-create-name / genre-create-display-name / genre-create-submit

interface MockGenre {
  name: string;
  display_name: string;
  role: string;
  enabled: boolean;
  bpm_min: number | null;
  bpm_max: number | null;
  description: string;
  created_at: string;
  updated_at: string;
}

const TS = "2026-06-01T00:00:00+09:00";

function mk(name: string, display: string, role: string): MockGenre {
  return {
    name,
    display_name: display,
    role,
    enabled: true,
    bpm_min: 80,
    bpm_max: 100,
    description: "",
    created_at: TS,
    updated_at: TS,
  };
}

function seedGenres(): MockGenre[] {
  return [
    mk("lo-fi-hip-hop", "Lo-Fi Hip Hop", "main"),
    mk("future-garage", "Future Garage", "experiment"),
    mk("vaporwave", "Vaporwave", "extension"),
  ];
}

const _LADDER = ["experiment", "extension", "main"];

/** genres 系 API をモックする。 state は閉包で保持し create/promote/demote/disable で書き換える。 */
async function mockGenresApi(page: Page): Promise<void> {
  const genres = seedGenres();
  const find = (path: string): MockGenre | undefined => {
    const parts = path.split("/");
    const name = parts[parts.length - 2]; // .../genres/{name}/{action}
    return genres.find((g) => g.name === name);
  };

  await page.route(/\/api\/backend\/genres/, async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (method === "POST" && path.endsWith("/promote")) {
      const g = find(path);
      if (!g) return route.fulfill({ status: 404, body: "{}" });
      g.role = _LADDER[Math.min(_LADDER.indexOf(g.role) + 1, _LADDER.length - 1)];
      g.enabled = true;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(g) });
    }

    if (method === "POST" && path.endsWith("/demote")) {
      const g = find(path);
      if (!g) return route.fulfill({ status: 404, body: "{}" });
      g.role = _LADDER[Math.max(_LADDER.indexOf(g.role) - 1, 0)];
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(g) });
    }

    if (method === "POST" && path.endsWith("/disable")) {
      const g = find(path);
      if (!g) return route.fulfill({ status: 404, body: "{}" });
      g.enabled = false;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(g) });
    }

    if (method === "POST" && path.endsWith("/genres")) {
      const body = request.postDataJSON() as Partial<MockGenre>;
      const created = mk(
        String(body.name),
        String(body.display_name),
        body.role ?? "experiment",
      );
      genres.push(created);
      return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(created) });
    }

    if (method === "GET" && path.endsWith("/genres")) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: genres, total: genres.length }),
      });
    }

    return route.continue();
  });
}

test.describe("[FR-037/FR-038] ジャンル管理画面 (US3)", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
    await mockGenresApi(page);
  });

  test("ジャンルが role 別に一覧表示される", async ({ page }) => {
    await page.goto("/genres");

    await expect(page.getByTestId("genres-table")).toBeVisible();
    await expect(page.getByTestId("genre-role-lo-fi-hip-hop")).toHaveText("主力");
    await expect(page.getByTestId("genre-role-future-garage")).toHaveText("実験");
    await expect(page.getByTestId("genre-role-vaporwave")).toHaveText("拡張");
  });

  test("実験ジャンルを昇格(採用)すると role が 1 段上がる", async ({ page }) => {
    await page.goto("/genres");
    await page.getByTestId("genre-promote-future-garage").click();
    await expect(page.getByTestId("genre-role-future-garage")).toHaveText("拡張");
  });

  test("ジャンルを降格すると role が 1 段下がる", async ({ page }) => {
    await page.goto("/genres");
    await expect(page.getByTestId("genre-role-lo-fi-hip-hop")).toHaveText("主力");
    await page.getByTestId("genre-demote-lo-fi-hip-hop").click();
    await expect(page.getByTestId("genre-role-lo-fi-hip-hop")).toHaveText("拡張");
  });

  test("最上位は昇格不可 / 最下位は降格不可", async ({ page }) => {
    await page.goto("/genres");
    // main は昇格不可、 experiment は降格不可。
    await expect(page.getByTestId("genre-promote-lo-fi-hip-hop")).toBeDisabled();
    await expect(page.getByTestId("genre-demote-future-garage")).toBeDisabled();
  });

  test("ジャンルを無効化すると状態が無効になる", async ({ page }) => {
    await page.goto("/genres");
    const row = page.getByTestId("genre-row-vaporwave");
    await expect(row).toContainText("有効");
    await page.getByTestId("genre-disable-vaporwave").click();
    await expect(row).toContainText("無効");
  });

  test("ジャンルを手動で新規追加できる(既定 role=実験)", async ({ page }) => {
    await page.goto("/genres");

    await page.getByTestId("genre-create-name").fill("ambient");
    await page.getByTestId("genre-create-display-name").fill("Ambient");
    await page.getByTestId("genre-create-submit").click();

    // 追加後に一覧へ反映され、 既定 role=experiment(実験)で表示される。
    await expect(page.getByTestId("genre-row-ambient")).toBeVisible();
    await expect(page.getByTestId("genre-role-ambient")).toHaveText("実験");
  });
});
