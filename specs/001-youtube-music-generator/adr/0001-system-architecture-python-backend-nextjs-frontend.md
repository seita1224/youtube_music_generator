# ADR-0001: バックエンド = Python、管理UI = Next.js の2層構成

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, frontend, infra

## 背景

本システムは「GPU 上のバッチ処理(音楽生成・画像生成・LLM・動画合成・投稿)」と「人間が状況を確認・介入する管理画面」の2つの責務を持つ。
両者を同一言語に寄せると、片方の生産性が大きく落ちる。

- バッチ側の主要技術スタックは全て Python 中心
  - ACE-Step (PyTorch)、Stable Diffusion 系(diffusers)、Whisper 系、AcoustID/Chromaprint(pyacoustid)、google-api-python-client、slack_sdk
- 管理UI は要件3〜7(可視化・プロンプト介入・動画管理・アナリティクス)で SPA 相当の UI が必要
  - React/Next.js のエコシステム(shadcn/ui、tanstack/query、recharts 等)が厚い

## 決定

- **バックエンド = Python**(uv 管理)。ジョブ実行・GPU処理・外部API呼び出しを担う
- **管理UI = Next.js (App Router)**。バックエンドが提供する REST/SSE を呼ぶ
- バックエンドは **FastAPI** で API を露出(SSE で進捗ストリーム配信) — 詳細根拠は ADR-0009
- 通信境界: REST(CRUD・設定)+ SSE(リアルタイム進捗・ログ)

## 結果

### 良い影響

- 各層で標準的な技術が使え、生産性が高い
- ML推論と UI の関心が綺麗に分離される
- 管理UI は別マシン(ブラウザ)から接続可能、運用が柔軟

### 悪い影響・トレードオフ

- 2言語のメンテが必要(型定義の二重管理)
  - 緩和: OpenAPI スキーマ生成 + `openapi-typescript` で TS 型を自動生成
- ローカル起動時にバックエンド + フロントエンドの2プロセス管理が必要
  - 緩和: docker-compose or Makefile で一発起動

### 受容したリスク

- バックエンドとフロントエンドのバージョンドリフト → CI で OpenAPI 整合性チェック

## 検討した代替案

- **Python オンリー(Streamlit / Gradio):** UI の自由度が低い、要件3〜7のリッチな画面に不向き。不採用。
- **Node.js オンリー:** ACE-Step / SDXL の Python エコシステムから外れ、推論を別プロセスで呼ぶオーバーヘッドが大きい。不採用。
- **モノレポ + 共有型定義(tRPC等):** 過剰な抽象化、Python と TS をまたぐ tRPC は実用的でない。不採用。

## 関連

- ../requirements.md §システム構成
- ADR-0002: マスターLLM 抽象化層
- ADR-0007: dryrun モード(両層をまたぐ機能)
