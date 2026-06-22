import { test, expect, type Page, type Route } from "@playwright/test";

// T092 e2e: dryrun レビュー画面の critical user flow (US2)。
//
// UI 契約 (Playwright と画面エージェント一致用) に厳密準拠する:
//  - ルート: 一覧 /dryrun、詳細 /dryrun/[id]
//  - data-testid: dryrun-card / dryrun-state-badge / dryrun-video /
//    dryrun-approve-btn / dryrun-reject-btn / dryrun-reject-reason /
//    dryrun-reject-confirm-btn
//  - ボタン文言: 承認="承認して投稿" / 却下を開く="却下" / 却下確定="却下を確定"
//  - 状態ラベル: pending=保留中 / approved=承認済 / rejected=却下 /
//    auto_expired=期限切れ / posted=投稿済
//  - 却下理由は min 4 文字・必須 (空送信はボタン無効 or バリデーションエラー)
//
// 第一はフルスタック前提のセレクタ検証。 ただし実 API が無くても落ちないよう、
// バックエンド応答 (`/api/backend/dryrun/...`) と動画ストリームを page.route で
// mock し、 ページ実装のセレクタ・文言・遷移・mutation を決定論的に検証する。
//
// Basic 認証 (ADR-0013, LAN 内) は test.use({ httpCredentials }) で付与する。
// env からクレデンシャルを読み、 未設定時はデフォルト admin/admin にフォールバックする
// (ローカル backend の既定。 ソースに秘密はハードコードしない)。

// ---------------------------------------------------------------------------
// Basic 認証クレデンシャル (env 優先。 ブラウザ Basic セッション再利用のため
// httpCredentials を describe スコープで付与する)。
// ---------------------------------------------------------------------------
const BASIC_AUTH_USER =
  process.env.E2E_BASIC_AUTH_USER ??
  process.env.NEXT_PUBLIC_BASIC_AUTH_USER ??
  "admin";
const BASIC_AUTH_PASSWORD =
  process.env.E2E_BASIC_AUTH_PASSWORD ??
  process.env.NEXT_PUBLIC_BASIC_AUTH_PASSWORD ??
  "admin";

// ---------------------------------------------------------------------------
// mock データ (契約 schema に一致。 lib/api/dryrun.ts の型と整合)。
// ---------------------------------------------------------------------------
const PENDING_ID = "11111111-1111-4111-8111-111111111111";
const POST_ID = "22222222-2222-4222-8222-222222222222";

type DryrunState =
  | "pending"
  | "approved"
  | "rejected"
  | "auto_expired"
  | "posted";

interface MockDryrunOutput {
  readonly id: string;
  readonly post_id: string;
  state: DryrunState;
  readonly video_uri: string;
  reject_reason?: string;
  readonly created_at: string;
  reviewed_at?: string;
}

function pendingOutput(): MockDryrunOutput {
  return {
    id: PENDING_ID,
    post_id: POST_ID,
    state: "pending",
    video_uri: "file:///srv/ymg/outputs/sample.mp4",
    created_at: "2026-06-15T00:00:00Z",
  };
}

// 1x1 の最小バイト列。 <video> 要素のソース要求 (206/200) を満たすためのダミー。
// 実ファイルではないため再生はしないが、 要素のマウントとリクエスト発火は検証できる。
const FAKE_MP4_BYTES = Buffer.from([0x00, 0x00, 0x00, 0x18]);

// ---------------------------------------------------------------------------
// route mock 配線。 backend プロキシ (/api/backend/dryrun/...) を傍受する。
// state は引数のオブジェクトを書き換えて approve/reject 後の再取得に反映する。
// ---------------------------------------------------------------------------
async function mockDryrunApi(
  page: Page,
  output: MockDryrunOutput,
): Promise<void> {
  await page.route("**/api/backend/dryrun/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    // 動画ストリーム: GET /dryrun/outputs/{id}/video
    if (path.endsWith(`/dryrun/outputs/${output.id}/video`)) {
      await route.fulfill({
        status: 200,
        contentType: "video/mp4",
        headers: { "accept-ranges": "bytes" },
        body: FAKE_MP4_BYTES,
      });
      return;
    }

    // 承認: POST /dryrun/outputs/{id}/approve
    if (
      method === "POST" &&
      path.endsWith(`/dryrun/outputs/${output.id}/approve`)
    ) {
      output.state = "posted";
      output.reviewed_at = "2026-06-15T01:00:00Z";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(output),
      });
      return;
    }

    // 却下: POST /dryrun/outputs/{id}/reject
    if (
      method === "POST" &&
      path.endsWith(`/dryrun/outputs/${output.id}/reject`)
    ) {
      const payload = request.postDataJSON() as { reason?: string };
      output.state = "rejected";
      output.reject_reason = payload?.reason;
      output.reviewed_at = "2026-06-15T01:00:00Z";
      // backend reject は 200 + 更新後 DryrunOutput を返す (api/dryrun.py:210
      // DryrunOutputResponse / contract responses 200)。 approve と同形。
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(output),
      });
      return;
    }

    // 詳細取得: GET /dryrun/outputs/{id}
    if (
      method === "GET" &&
      path.endsWith(`/dryrun/outputs/${output.id}`)
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(output),
      });
      return;
    }

    // 一覧取得: GET /dryrun/outputs (?state=...)
    if (method === "GET" && path.endsWith("/dryrun/outputs")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [output] }),
      });
      return;
    }

    await route.continue();
  });
}

test.describe("[FR-061/FR-063] dryrun レビュー画面 (US2)", () => {
  // LAN 内 Basic 認証セッションを付与 (ADR-0013)。 同一オリジン proxied パスの
  // <video> もこのセッションを再利用する。
  test.use({
    httpCredentials: {
      username: BASIC_AUTH_USER,
      password: BASIC_AUTH_PASSWORD,
    },
  });

  test("一覧→詳細→承認: pending を承認して投稿済になる", async ({ page }) => {
    const output = pendingOutput();
    await mockDryrunApi(page, output);

    // 1. /dryrun を開く → dryrun-card 表示
    await page.goto("/dryrun");
    const card = page.getByTestId("dryrun-card").first();
    await expect(card).toBeVisible();

    // 一覧カードの状態バッジは保留中
    await expect(
      card.getByTestId("dryrun-state-badge"),
    ).toContainText("保留中");

    // 2. カードから詳細へ遷移
    await card.click();
    await expect(page).toHaveURL(new RegExp(`/dryrun/${PENDING_ID}$`));

    // 3. dryrun-video 表示
    const video = page.getByTestId("dryrun-video");
    await expect(video).toBeVisible();

    // 4. dryrun-approve-btn (承認して投稿) 押下
    const approveBtn = page.getByTestId("dryrun-approve-btn");
    await expect(approveBtn).toHaveText("承認して投稿");
    await approveBtn.click();

    // 5. 承認済 (posted=投稿済) の表示に更新される
    await expect(
      page.getByTestId("dryrun-state-badge"),
    ).toContainText("投稿済");
  });

  test("一覧→詳細→却下: 理由を入力して却下になる", async ({ page }) => {
    const output = pendingOutput();
    await mockDryrunApi(page, output);

    await page.goto(`/dryrun/${PENDING_ID}`);
    await expect(page.getByTestId("dryrun-video")).toBeVisible();

    // 却下を開く ("却下" ボタン)
    const rejectOpenBtn = page.getByTestId("dryrun-reject-btn");
    await expect(rejectOpenBtn).toHaveText("却下");
    await rejectOpenBtn.click();

    // 却下理由入力 (min 4 文字必須) → 確定
    const reasonInput = page.getByTestId("dryrun-reject-reason");
    await expect(reasonInput).toBeVisible();
    await reasonInput.fill("音質が低い");

    const confirmBtn = page.getByTestId("dryrun-reject-confirm-btn");
    await expect(confirmBtn).toHaveText("却下を確定");
    await confirmBtn.click();

    // 却下 (rejected=却下) の表示に更新される
    await expect(
      page.getByTestId("dryrun-state-badge"),
    ).toContainText("却下");
  });

  test("却下: 理由が短い (4 文字未満) と確定できない", async ({ page }) => {
    const output = pendingOutput();
    await mockDryrunApi(page, output);

    await page.goto(`/dryrun/${PENDING_ID}`);
    await page.getByTestId("dryrun-reject-btn").click();

    const reasonInput = page.getByTestId("dryrun-reject-reason");
    await expect(reasonInput).toBeVisible();

    // 空 / 4 文字未満では確定ボタンが無効 (or バリデーションで先に進めない)。
    const confirmBtn = page.getByTestId("dryrun-reject-confirm-btn");
    await expect(confirmBtn).toBeDisabled();

    await reasonInput.fill("短い"); // 2 文字
    await expect(confirmBtn).toBeDisabled();

    await reasonInput.fill("十分な理由"); // 5 文字 → 有効化
    await expect(confirmBtn).toBeEnabled();
  });
});
