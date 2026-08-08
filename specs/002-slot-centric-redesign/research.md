# Research: 枠中心モデルへの再設計

**Date**: 2026-08-07

設計判断そのものは ADR-0041〜0050 で確定済み。本書は **実装に入る前に外部事実の確認が必要だった項目** と、ADR が実装に委ねた技術選定を記録する。

---

## R-1: YouTube API quota の現行値(ADR-0045 が再確認を要求)

**確認結果(確認日 2026-08-07)**: ADR-0045 記載の「`videos.insert` = 1600 units、10,000 units/日 → 実効 6 本/日」は**旧値**。

- Google 公式 Quota Calculator(developers.google.com/youtube/v3/determine_quota_cost、2026-06 改版)によると、現行の既定割当は「`search.list` 100 call/日 + `videos.insert` 100 call/日(各専用バケット、1 call = 1 quota)+ その他エンドポイント合算 10,000 units/日」。`videos.insert` は共有プールから独立した
- 経緯: 2025-12-04 に `videos.insert` のコストが約 1600 → 約 100 units に引き下げられ、2026-06-01 の改版で専用バケット方式に変更された(YouTube Data API Revision History、二次情報源: bundle.social / blotato.com / getphyllo.com の 2026 年ガイド)
- **注意**: 文書化されていない「Video Uploads per day」制限による 429 の報告あり(google-api-python-client issue #2753、2026-05)。API quota に余裕があってもチャンネル側の上限で失敗しうる

**設計への反映**:

- API quota は本システムの投稿規模(数本/日)では実質ボトルネックでなくなった。ただし非公開のチャンネル側上限が存在する報告があるため、**編成表の警告閾値(FR-015)は設定値 `PUBLISH_WARN_PER_DAY` とし、初期値 6 を維持**する(保守的初期値。運用実測で引き上げ)
- quota 枯渇待機(`publish_block_reason=quota_exhausted`)の仕組みは 429 / quotaExceeded 全般への対応として残す
- ADR-0045 の quota 記述は実装完了時に増分更新する(受容済みリスク「Google の割当変更で変動しうる」の範囲内)

## R-2: Slack ボタン / コマンド受信方式(ADR-0043 / 0044 が要求)

**課題**: 旧実装は Incoming Webhook(送信専用)のみ。新設計は Slack **から** の操作(取り下げボタン、`stop` / `pause-publishing` / `resume`)を要求するが、本システムは LAN 内で公開 HTTP エンドポイントを持たない。

**確認結果(確認日 2026-08-07、docs.slack.dev/apis/events-api/using-socket-mode)**: Socket Mode を使えば公開 Request URL なしで Events API とインタラクティブ機能(ボタン等)を WebSocket 経由で受信できる。App-Level Token(`xapp-`、`connections:write` scope)が必要。ペイロードは 3 秒以内に ack する必要がある。単一レプリカ・低トラフィックの内部ツール向けとされており、本システムの構成(個人運用・単一プロセス)に合致する。

**Decision**: Slack Bolt for Python + Socket Mode を backend プロセス内(別 asyncio task)で常駐させる。

- 通知(送信)は既存の webhook 方式を継承しつつ、ボタン付き通知(公開通知 + 取り下げ導線)は bot token の `chat.postMessage` + Block Kit に移行する
- 操作コマンドはボタンまたはスラッシュコマンド相当のメッセージ操作で受け、backend の system_state / withdraw API と同一のサービス層を呼ぶ(経路を分けない)

**Alternatives**: 公開エンドポイント + Events API(トンネル / リバースプロキシが必要になり、LAN 内前提と攻撃面の点で不採用)。Slack CLI ホスト型アプリ(Deno、backend と分離してしまい状態参照が二重化するため不採用)。

## R-3: 状態機械 / 物化の property test

**Decision**: hypothesis を導入し、(a) 遷移表 T1〜T18 以外の遷移が全組合せで拒否されること、(b) INV-1〜INV-6 が任意の操作列後に保持されること、(c) 物化の決定論(同一パターン・例外・日付 → 同一枠集合)を property test で検証する。

**Rationale**: ADR-0045 は Lean 形式化を主対象と明記するが、spec の Out-of-Scope で Lean 実施は別作業とした。10 状態 × 18 遷移 + 属性(current_stage / publish_block_reason)の組合せは example-based テストでは網羅しきれない。

**Alternatives**: example-based のみ(網羅漏れリスク)、Lean を今やる(実装が長期停滞。別作業に切り出し済み)。

## R-4: 旧 DB からの移行対象(ADR-0050 (3) の具体化)

**Decision**: 1 回きりのスクリプト `ymg migrate-legacy`(旧 DB への読み取り専用接続)で以下のみ移行する。

| 旧テーブル | 新テーブル | 内容 |
|---|---|---|
| `videos`(公開済みのみ) | `videos` | youtube_video_id / 公開日時 / ジャンル(legacy 行は slot_id NULL) |
| `analytics_daily` | `analytics_daily` | 実績値(ADR-0047 の目的関数が使用) |
| `genres` | `genres` | 名前 / role / 一時停止状態 |
| `model_pricing` | `model_pricing` | LLM 単価表 |
| `oauth_credentials` | `oauth_credentials` | Fernet 暗号化済みトークン(鍵は同一 `.env` を継続) |

plans / dryrun_outputs / posts / gpu_jobs / job_history / usage_log / comments は移行しない(ADR-0050。usage_log は月次集計済みのコスト履歴のみ必要なら CSV 化して保管、DB には持ち込まない)。

## R-5: CLI 実装(`ymg stop` / `pause-publishing` / `resume` / `migrate-legacy`)

**Decision**: Typer で `ymg` コマンドを backend パッケージの entry point として実装し、backend API(セッションではなくローカル管理トークン)経由で system_state を操作する。API 経由にするのは、参照点・監査ログ・Slack 通知を単一のサービス層に通すため(ADR-0044「ここを通らない実行経路を作らない」)。

**Alternatives**: DB 直接更新(参照点は守れるが監査 / 通知の経路が二重化)、Makefile + curl(旧 panic-stop 方式。引数検証・対話確認が貧弱)。Typer / click / argparse は lockfile 確定時に最新安定版を確認する。

## R-6: Alembic マイグレーションチェーン

**Decision**: 新スキーマは**新しいベースライン revision から開始**する(旧 `001_initial` チェーンを引き継がない)。新 DB(例: `ymg_v2`)を作成し、旧 DB はリネームして凍結アーカイブとする。恒久互換層なし(ADR-0050)。

**Rationale**: 旧概念(plans / dryrun)のテーブルを ALTER で作り替えるより、空 DB + 実績のみ移行(R-4)のほうが単純で、down migration を書かない方針(憲法 Deploy Discipline)とも整合する。

## R-7: GPU 実行順(EDF)の実装位置

**Decision**: EDF キューは backend 側(`domain/production/edf_queue.py`)に置き、GPU worker へは常に 1 ジョブずつ投入する。GPU worker の API 契約(`contracts/gpu-worker-api.yaml`)は**無変更**で維持する。

**Rationale**: 順序付けの根拠(公開予定時刻・物化順)は枠 = backend の集約情報であり、GPU worker に渡すと契約が太る。ADR-0031 のクラウド移行可能性(worker は差し替え可能な計算資源)も保ちやすい。

**Alternatives**: GPU worker 側に優先度キュー(契約変更が必要、worker がドメイン知識を持ってしまう。不採用)。
