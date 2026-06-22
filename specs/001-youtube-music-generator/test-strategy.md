# テスト実装方針 (Test Strategy)

**対象**: `001-youtube-music-generator`
**正本仕様**: [spec.md](./spec.md)(FR-001〜FR-123 / US1〜US7 / SC-001〜SC-010)
**トレーサビリティ**: [traceability.md](./traceability.md)(FR → テスト対応表)

> このドキュメントは「どういう方針でテストを実装したか」を後から検証できる形で残すためのもの。
> 個々の「どのテストがどの FR を満たすか」は各テストの `@pytest.mark.fr(...)` / フロントのテスト名
> `[FR-xxx]` と [traceability.md](./traceability.md) を参照。

---

## 1. 目的

1. **全仕様の網羅** — spec.md の機能要件 FR を漏れなくテストで担保する。自動テストが原理的に
   不可能な要件(GPU 実機生成・外部 SaaS 実投稿・技術選定・cron/デプロイ)は、その理由と
   代替検証手段を明示したうえで網羅対象から除外する(=「黙って未テスト」を作らない)。
2. **仕様↔テストの追跡可能性** — どのテストがどの FR を満たすかを、人間にもツールにも分かる形で
   機械可読に紐付ける。
3. **網羅の自動保証** — 新しい FR が spec.md に増えたのにテストが無い、という状態を CI で機械検出する。

---

## 2. トレーサビリティ機構 (どのテストがどの仕様か)

### 2.1 backend (pytest): `fr` マーカー

各テストが満たす FR を `@pytest.mark.fr("FR-xxx", ...)` で宣言する。`pyproject.toml` に登録済み。

```python
@pytest.mark.fr("FR-006", "FR-007")
def test_missing_synthetic_media_flag_blocks_upload(...):
    """FR-006/FR-007: containsSyntheticMedia 未設定なら投稿前バリデーションで停止する。"""
    ...
```

- マーカー引数 = その test が検証する FR ID。複数可。
- docstring 先頭にも `FR-xxx:` を書き、テスト出力・コードリーディング双方で意図が分かるようにする。
- 利点: `pytest -m "fr"` で追跡対象だけ走らせる / マーカー引数を静的走査して網羅表を生成できる。

### 2.2 frontend (vitest / Playwright): テスト名プレフィックス

JS にはマーカーが無いため、`test()` / `describe()` のタイトル先頭に `[FR-xxx]` を付ける。
テストランナーの出力にそのまま FR が出るため、失敗時に「どの仕様が壊れたか」が即分かる。

```ts
test("[FR-085] 認証ヘッダを /api/backend 配下の全リクエストへ注入する", () => { ... });
```

### 2.3 網羅ゲート (メタテスト)

`backend/tests/test_requirement_coverage.py` が CI ゲートとして機能する:

1. `spec.md` を静的に走査して **全 FR ID の集合**(単一の正本)を得る。
2. backend 全テストの `fr` マーカー引数 + frontend テストの `[FR-xxx]` を静的走査して
   **テストが宣言する FR 集合**を得る。
3. `MANUAL_FRS`(自動テスト不能な FR + その理由の明示リスト)を引いたうえで、
   **未カバーの FR が 1 つでもあれば fail** する。
4. 逆に、存在しない FR を指すマーカー / 古くなった `MANUAL_FRS` エントリも fail させる(腐敗検出)。

これにより「spec に FR を足したらテストも足さないと CI が赤」になる。網羅は人手の注意ではなく
仕組みで保証する。

---

## 3. テスト種別とピラミッド

| 層 | 場所 | ランナー | 役割 |
|----|------|----------|------|
| backend unit | `backend/tests/unit/` | pytest | ドメインロジック・API ハンドラ(mock 境界)。最多。 |
| backend critical | `backend/tests/critical/` | pytest (`-m critical`) | Constitution II の critical path。**カバレッジ 100% 強制**。 |
| backend integration | `backend/tests/integration/` | pytest (`-m integration`) | 実 PostgreSQL を使う結線検証。専用 DB `ymg_test` に隔離。 |
| backend meta | `backend/tests/test_requirement_coverage.py` | pytest | 上記 §2.3 の網羅ゲート。 |
| frontend unit | `frontend/tests/unit/` | vitest (jsdom) | lib の純ロジック(auth/client/sse/hook)。 |
| frontend e2e | `frontend/tests/e2e/` | Playwright (chromium) | 画面の critical user flow。backend は `page.route` でモック。 |

「多数の小さな速い unit + 要所の integration/e2e」というピラミッドを採る。GPU・外部 SaaS は
契約境界(HTTP/関数)で mock し、決定論的に回す。

---

## 4. 採用したベストプラクティス(と根拠)

- **Test-First (TDD)** — Constitution II(NON-NEGOTIABLE)。critical path はテスト先行・実装後追い。
  → [.specify/memory/constitution.md](../../.specify/memory/constitution.md)
- **critical path 100% カバレッジ** — Constitution II。`make test-critical` と CI で
  `--cov-fail-under=100` を強制。安全・コンプラに直結する 7 モジュールが対象。
- **AAA (Arrange-Act-Assert)** — 各テストは「準備→実行→検証」で構造化し、1 テスト 1 観点を原則とする。
- **決定論性** — 時刻・乱数・ネットワークを排除する。
  - 時間依存(SSE 再接続バックオフ等)は **fake timers** で制御(`vi.useFakeTimers`)。
  - HTTP は backend で `respx`(httpx mock)、frontend で `page.route` / `authFetch` mock。
  - `Date.now()`/`Math.random()` に依存するロジックは引数注入でテスト可能にする。
- **テスト隔離** — integration は専用 DB `ymg_test` を `conftest.py` が起動時生成し、開発 DB(`ymg`)を
  決して破壊しない(autouse fixture が `POSTGRES_DB` を実行時上書き)。
  → [backend/tests/integration/conftest.py](../../backend/tests/integration/conftest.py)
- **境界モックは公開契約に一致させる** — モックの応答形・ステータスは実 backend / `contracts/` の
  スキーマに厳密一致させる(乖離したモックは「通るのに本番は壊れる」を生むため)。
- **実 DB 結線まで検証** — mypy strict + mocked unit を通っても、並列実装の結線ズレ(境界不一致・
  ファイル名・絶対 URI)は実 Postgres でしか露見しない。重要フローは integration を必ず通す。
- **E2E は自己完結 + CI 実行可能** — Playwright の `webServer` で `next dev` を専用ポートで自動起動し、
  実 backend 無し(全 `page.route` モック)で CI でも走る。

---

## 5. 実行方法

```bash
# backend
make test                # 全 pytest
make test-critical       # critical path のみ + カバレッジ 100% ゲート
cd backend && uv run pytest -m fr                 # FR タグ付きテストのみ
cd backend && uv run pytest tests/test_requirement_coverage.py   # 網羅ゲート
cd backend && uv run pytest -m integration        # 実 DB(ymg_test)結線

# frontend
cd frontend && npm run test       # vitest (unit)
cd frontend && npm run test:e2e   # Playwright (e2e, next dev 自動起動)
```

CI(`.github/workflows/ci.yml`): backend は ruff / mypy / pytest / critical 100% / 網羅ゲート、
frontend は lint / typecheck / vitest / Playwright を実行する。

---

## 6. インフラ/デプロイ系 FR と仕様乖離の扱い

### 6.1 インフラ系は「静的契約テスト」で担保(MANUAL_FRS は空)

cron / systemd / Makefile / シェル / ビルド系の FR(FR-080/081/084/086/090/093/120–123)は pytest で
「実行」検証できないが、 `backend/tests/test_infra_contracts.py` がソース(Makefile / infra スクリプト /
CI / .gitignore / 依存定義)への**静的アサーション**で契約を担保する。 これは技術選定の自己言及ではなく
**退行ガード**として機能する(例: `make migrate` の pre-dump 削除、 backup の `.env` 混入、 CI の
`docker build` 削除、 OAuth scope 削除を検出して落ちる)。

結果として網羅ゲートの `MANUAL_FRS` は**空**= spec.md の全 FR がテストで説明される。 GPU 実機推論・
実投稿は契約境界(worker mock / 関数境界)まで自動検証し、 実バイナリの生成品質のみ quickstart.md /
MVP checklist の手動確認に委ねる(機能要件自体はテスト済み)。 自動化不能な FR が将来生じたら
`MANUAL_FRS` に理由付きで登録する運用。

### 6.2 仕様↔実装の乖離は調査→相談→修正で解消

テスト網羅の過程で spec.md と実装の不一致を 4 件発見した(FR-100 のスコープ不足、 FR-113 の
scheduler 自動停止、 FR-114 の Slack prefix 文言、 FR-051 のサブタイトル/絵文字制約)。 まず
**spec どおりを期待する test を書いて `@pytest.mark.xfail` で「緑のまま未充足」を記録**し、 ユーザー相談で
方針を確定後、 **実装を修正して xfail を解消**した(詳細・判断は [traceability.md](./traceability.md)
「仕様↔実装の乖離」節)。 この「乖離は黙って通さず、 xfail で見える化 → 判断 → 解消」が本プロジェクトの
乖離対応フロー。

## 7. 網羅状況(2026-06-22 時点)

- spec.md 機能要件 **FR 70 件すべて**にマーカー付きテストが存在(`test_requirement_coverage.py` が保証)。
- backend: unit+critical 全 green、critical path **100% カバレッジ**維持、mypy strict / ruff クリーン。
- 発見した仕様↔実装の乖離 4 件はすべて実装修正で解消(残る xfail は FR-072 の取得例外経路 1 件のみ)。
- frontend: vitest **33**、Playwright e2e **14**(自己完結・CI 実行可能)。
- 自動テスト対象外として除外した FR は **0 件**。
