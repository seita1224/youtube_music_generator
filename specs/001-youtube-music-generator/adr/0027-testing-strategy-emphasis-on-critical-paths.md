# ADR-0027: テスト方針 = メリハリ型(クリティカルパス厳格、その他 best effort)

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, frontend, ops, qa

## 背景

1人運用・実装速度重視 vs YouTube への自動投稿 → 回帰バグが BAN リスクと直結する。
一般的なテストピラミッドだけでは、GPU 推論・外部 API・LLM の特性に合わない。

## 決定

クリティカルパスは厳格に、それ以外は best effort のメリハリ型を採用。

### 1. クリティカルパス(カバレッジ 100% 目標、単体 + 結合 必須)

以下は **コンプライアンス・セキュリティに直結** するため、絶対に通すテストを書く:

- **AcoustID プレチェック**(ADR-0005)— 指紋一致時の動作、再生成ロジック、漏れ時の挙動
- **`containsSyntheticMedia` バリデーション**(ADR-0020)— 投稿時に必ず `true` であること、未設定時の停止挙動
- **OAuth トークンの暗号化・復号化**(ADR-0012)— Fernet 暗号化が正しく機能、鍵未設定時の挙動
- **LLM 構造化出力のバリデーション**(ADR-0018)— Pydantic 検証、リトライ、N回失敗時の Slack 通知
- **ディレクティブパーサ**(ADR-0017)— 変数参照 / LLM 生成 の判別、エスケープ、ネスト禁止

### 2. 重要パス(モック + 結合、カバレッジ 70% 目標)

- **GPU 推論**(ACE-Step / SDXL)— モックで API 契約をテスト、実推論は PoC とローカル GPU でのみ
- **YouTube API 投稿**(`videos.insert`)— `vcrpy` または `responses` でレスポンス録画再生
- **YouTube Analytics API**(ADR-0021)— 同上
- **LLM Provider 各実装**(ADR-0019)— `vcrpy` で録画再生、provider 切替テスト
- **APScheduler ジョブ**(ADR-0011)— 起動・状態遷移・retention ジョブ
- **dryrun ライフサイクル**(ADR-0025)— 状態遷移と自動削除

### 3. その他(best effort、全体カバレッジ 60〜70%)

- ユーティリティ関数・データ変換・ログフォーマット等
- 100% 必須ではないが、書ける範囲で書く

### 4. 管理UI E2E(Playwright)

- **必須シナリオ:**
  - dryrun 承認 → 本番投稿パスに移行
  - 改善計画レビュー → 翌日反映
  - ジョブ手動トリガ
- それ以外は best effort

### 5. 静的検査

- **型チェック**: `pyright`(`mypy` でも可)を CI に組み込む
- **lint**: `ruff`(format + lint 統合、速い)
- **secrets スキャン**: `gitleaks` か `trufflehog`(`.env` の誤コミット検出)

## ツール

- 単体・結合: `pytest` + `pytest-asyncio` + `pytest-mock` + `coverage`
- 録画再生: `vcrpy` または `pytest-recording`(LLM・YouTube API)
- モック: `unittest.mock` + `pytest-mock`、GPU 推論は `MagicMock` で出力固定
- E2E: `playwright` + `pytest-playwright`
- 型: `pyright` または `mypy`
- フォーマット・lint: `ruff`

## CI 環境

- GitHub Actions(private リポでも一定無料枠)
- GPU テストは **CI で動かない** → モック必須、本物 GPU テストは **ローカル GPU マシン上でフラグ切替**(`pytest -m gpu`)
- secrets: GitHub Actions Secrets で API キー管理(`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等、ただし VCR で録画済みなら不要)

## 結果

### 良い影響

- BAN リスクと直結するコンプライアンス系は厚く保護
- 重要外部 API は録画再生で速くかつ安定
- 全体の実装速度は best effort 部分で確保
- 静的検査(型・lint・secrets)で機械的に防げる事故は CI で潰す

### 悪い影響・トレードオフ

- VCR 録画の更新運用(API 仕様変更時に再録画が必要)
- E2E は管理UI 変更で壊れやすい → クリティカルシナリオに限定
- カバレッジ目標が部分ごとに違うため、CI レポート設計が必要(`coverage` のレポート分割)

### 受容したリスク

- best effort 部分での回帰バグは Slack 通知 + 構造化ログ(ADR-0023)で検知

## 検討した代替案

- **ピラミッド型カバレッジ 80% 均一:** 実装速度が落ちる、外部依存テストが手厚すぎてメンテ負担。不採用。
- **ミニマル / カバレッジ 50%:** コンプライアンス系の回帰バグ受容は許容できない。不採用。
- **トロフィー型(結合中心):** クリティカルパスの単体テスト不足リスク。不採用。

## 関連

- ADR-0005, ADR-0012, ADR-0017, ADR-0018, ADR-0020: クリティカルパスの対象
- ADR-0011: スケジューラジョブテスト
- ADR-0019: LLM Provider 録画再生
- ../requirements.md §テスト方針
