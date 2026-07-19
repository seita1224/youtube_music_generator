# ADR-0029: モノレポ + シンプルなディレクトリ分割

- **ステータス:** Accepted
- **日付:** 2026-05-26
- **決定者:** @seita
- **タグ:** infra / ops

## 背景

ADR-0001 でバックエンド(Python/FastAPI)とフロントエンド(Next.js)の2層構成が決定した。
リポジトリ構成として以下の選択肢があった。

- 1人運用、初期は本人のみが触る
- ADR / REQUIREMENTS / docs は単一の Markdown ツリーで管理したい
- バックエンドとフロントエンドは独立にデプロイ可能(ローカル GPU マシン上の systemd vs Vercel など)だが、API の型同期や CI 連携を考えると同居していたほうが楽

## 決定

**モノレポを採用し、`backend/` と `frontend/` の素直なディレクトリ分割で構成する。** Turborepo / Nx / pnpm workspaces などのモノレポツールは初期は導入しない。必要になった時点で再評価する。

想定する初期ツリー:

```text
youtube_music_generator/
├── README.md
├── requirements.md
├── CLAUDE.md
├── docs/
│   └── adr/
├── backend/                 # Python (uv + FastAPI)
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── src/
│   └── tests/
├── frontend/                # Next.js (App Router)
│   ├── package.json
│   ├── app/
│   └── ...
└── infra/                   # systemd unit, docker-compose, scripts (将来)
```

## 結果

### 良い影響

- ADR とコードが同じ git tree にあるため、決定と実装の対応がレビュー時に追いやすい
- バックエンド API スキーマと frontend クライアントの型同期を 1 PR にまとめられる
- CI を 1 本にまとめられる(backend/frontend の lint / 型 / テストを `paths:` フィルタで切り分け)
- リリース/ロールバックの粒度を統一できる(ADR-0006 の日次/週次サイクルと整合)
- Vercel は monorepo のサブディレクトリデプロイをネイティブサポートしているため、`frontend/` を Root Directory に指定するだけでデプロイ可能

### 悪い影響・トレードオフ

- リポジトリのファイル数が増え、`gh search` や `grep` の結果が増える
- backend と frontend の依存関係が「同じリポにある」だけで物理的には独立しているのに、 一見結合しているように見える可能性
- 反転コスト: 後から別リポに分けるのは `git filter-repo` で履歴ごと切り出せるため低〜中

### 受容したリスク

- 単一リポ単一障害点: GitHub 上のリポが消えると両方失われる(ADR-0026 のバックアップ方針でカバー)

## 検討した代替案

- **代替案B: 別リポ (`ymg-backend` + `ymg-frontend`):** 関心が物理的に完全分離されるが、ADR をどちらに置くか問題が発生し、API 契約変更時に 2 PR + 連携作業が必要になる。1人運用ではオーバーヘッド過多。不採用。
- **代替案C: モノレポ + Turborepo / Nx:** タスクキャッシュやリモートビルドの恩恵があるが、対象は backend(Python)+ frontend(TS)の2パッケージのみで、Python は Turborepo の主戦場ではない。導入コストが利益を上回ると判断。不採用。
- **代替案D: モノレポ + pnpm workspaces のみ:** TS 側だけならアリだが、現状 frontend は単一パッケージのため不要。

## 関連

- ADR-0001: バックエンド = Python、管理UI = Next.js
- ADR-0006: サイクル構造(日次+週次) — リリース粒度の根拠
- ADR-0026: バックアップ方針 — 単一リポ単一障害点の補完
- ../requirements.md §4 システム構成
