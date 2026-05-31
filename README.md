# youtube_music_generator

ローカル GPU (RTX 3090) 上で AI 生成楽曲動画を継続投稿する自動化システム。

- **音楽生成:** ACE-Step 1.5
- **画像生成:** Stable Diffusion 系
- **マスターLLM:** クラウドAPI(Claude / GPT 等、切替可)
- **指紋認識:** AcoustID + Chromaprint
- **バックエンド:** Python (FastAPI)
- **管理UI:** Next.js (App Router)

## ドキュメント

- [requirements.md](specs/001-youtube-music-generator/requirements.md) — 要件定義 v2(最新スナップショット)
- [specs/001-youtube-music-generator/adr/](specs/001-youtube-music-generator/adr/) — Architecture Decision Records
  - [0001 システム構成](specs/001-youtube-music-generator/adr/0001-system-architecture-python-backend-nextjs-frontend.md)
  - [0002 マスターLLM クラウドAPI](specs/001-youtube-music-generator/adr/0002-master-llm-cloud-api-with-abstraction-layer.md)
  - [0003 動画フォーマット 30分](specs/001-youtube-music-generator/adr/0003-video-format-30min-via-six-track-stitching.md)
  - [0004 投稿規模 段階拡大](specs/001-youtube-music-generator/adr/0004-posting-volume-staged-from-1-2-per-day.md)
  - [0005 AcoustID 事前チェック](specs/001-youtube-music-generator/adr/0005-content-id-pre-check-with-acoustid.md)
  - [0006 日次/週次サイクル](specs/001-youtube-music-generator/adr/0006-cycle-structure-daily-and-weekly.md)
  - [0007 dryrun モード](specs/001-youtube-music-generator/adr/0007-dryrun-mode-as-mvp-requirement.md)
  - [0008 LLM 責務範囲](specs/001-youtube-music-generator/adr/0008-llm-responsibility-scope.md)

## ステータス

PoC 前の設計フェーズ。詳細は [requirements.md §11 次のアクション](specs/001-youtube-music-generator/requirements.md#11-次のアクション) を参照。

ADR は 0035 件まで蓄積(`0035` は staged rollout 方針)。 仕様は [spec.md](specs/001-youtube-music-generator/spec.md)、 実装計画は [plan.md](specs/001-youtube-music-generator/plan.md) を参照。

## ライセンス

Private
