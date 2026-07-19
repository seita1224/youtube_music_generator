# ADR-0031: デプロイ手順(コンポーネント実行方式・デプロイフロー・オートスタート・ロールバック)

- **ステータス:** Accepted
- **日付:** 2026-05-26
- **決定者:** @seita
- **タグ:** infra / ops

## 背景

ADR-0009(FastAPI)、ADR-0010(PostgreSQL + pgvector)、ADR-0011(APScheduler in backend process)、ADR-0015〜0016(SDXL + ACE-Step GPU 推論)、ADR-0029(モノレポ)、ADR-0030(git 運用)を踏まえ、本番(ローカル GPU マシン / RTX 3090 / Ubuntu)上での実行方式とデプロイ手順を確定する。

前提:

- 1人運用、本機は自宅 LAN
- 動機 C(マネタイズ) + AI 動画コンプラ感応度が高い
- 将来 RunPod 等のクラウド GPU に切り替えたくなる可能性がある(明示要件)
- 停電 / kernel update 等での reboot は発生し得る

## 決定

### (1) コンポーネント実行方式 = ハイブリッド + GPU worker は HTTP API 契約 + Dockerfile 用意

| コンポーネント | 実行方式 |
|---|---|
| backend (FastAPI) | docker compose |
| frontend (Next.js) | docker compose |
| PostgreSQL (+ pgvector) | docker compose |
| **GPU worker** (ACE-Step + SDXL) | **host 直 + systemd**, ただし Dockerfile は repo に commit |

##### GPU worker のインターフェース契約(切り替え可能性を担保)

GPU worker は backend と独立したプロセスで、 **HTTP API + fsspec ストレージ** で疎結合する。

- backend → GPU worker は HTTP のみ(直接 import しない)
- 入出力ファイルは fsspec URI で受け渡し(`file://` / `s3://` 両対応、ADR-0022)
- backend は `GPU_WORKER_BASE_URL` 環境変数で worker の場所を切り替える

API 草案(後続 ADR で詳細化):

```text
POST /generate/music   { prompt, duration_sec, output_uri } → { job_id }
POST /generate/image   { prompt, output_uri }                → { job_id }
GET  /jobs/{job_id}                                          → { status, output_uri, vram_peak_mb }
GET  /health                                                 → { gpu_available, vram_free_mb }
```

##### RunPod 等への切り替え手順(設計上保証する)

1. GPU worker の `Dockerfile` で `docker build && docker push`
2. RunPod Pod or Serverless にデプロイ
3. backend の `GPU_WORKER_BASE_URL` を env で差し替え
4. 共有ストレージを fsspec URI 変更で S3 / R2 / B2 に移行(必要なら)
5. **backend / frontend のコードは無変更**

### (2) デプロイ実行手順 = Makefile 経由の手動 deploy

`Makefile` をデベロッパー向けコマンド表面とする。 内部実装は `scripts/*.sh` に分離してもよい。

主要ターゲット(初期):

```text
make up               # docker compose up -d + gpu worker start
make down             # 全停止
make deploy           # git pull → migrate → image rebuild → restart → health check
make migrate          # alembic upgrade head (前に pg_dump を取る)
make restart-backend  # docker compose restart backend
make restart-gpu      # systemctl restart ymg-gpu-worker
make logs             # docker compose logs -f
make panic-stop       # 後述、緊急停止
make help             # ターゲット一覧
```

deploy の本体:

```bash
make deploy:
  git fetch --prune
  git reset --hard origin/main
  make migrate          # pg_dump → alembic upgrade head
  docker compose build backend frontend
  docker compose up -d backend frontend
  cd gpu_worker && uv sync --frozen && sudo systemctl restart ymg-gpu-worker
  ./scripts/healthcheck.sh
```

healthcheck.sh は backend `/health`, frontend root, gpu worker `/health` を `curl -fsS` で叩いて全部 200 なら exit 0。

### (3) マシン再起動時の挙動 = API はオートスタート、scheduler は手動 enable

- `ymg-stack.service`(docker compose up を `ExecStart` に書く) を `systemctl enable`
- `ymg-gpu-worker.service` を `systemctl enable`
- どちらも `Restart=on-failure`
- **APScheduler の job 登録は DB の `app_state.scheduler_enabled` に依存**:
  - reboot 直後は **false 起動が原則**(運用初期は手動 ON を必須化)
  - 管理 UI に「scheduler を起動」ボタン
  - ON 時に DB を更新 + APScheduler に job 群を add、OFF 時に remove
- 投稿という不可逆アクションは「人間が一手挟む」運用とする(ADR-0007 dryrun 思想と整合)

将来的に運用が安定したら「scheduler_enabled の前回値を引き継ぐ」モードに切り替え可能(明示的 ADR が必要)。

### (4) ロールバック手順 = `git revert` + 緊急停止スクリプトを併設

##### コードロールバック

- down migration は書かない
- 戻し方:

```bash
git revert <bad-sha>
git push
make deploy
```

- DB スキーマの後退が必要な事故は年に数回想定、その時だけ手動 SQL で対応

##### DB ロールバック

- `make migrate` は **実行前に自動で `pg_dump`** を取り、 `backups/pre-migrate-<timestamp>.sql` に保存
- マイグレーション破損時は `make restore-db DUMP=...` で復旧(数分のダウンタイム許容)
- バックアップ保存先は ADR-0026 の local secondary disk と統合

##### `make panic-stop`(コンプラ事故対応の最小ツール)

ADR-0028 の `compliance` エラーカテゴリの即応動作として定義。

```text
make panic-stop:
  1. DB の scheduler_enabled = false に倒す
  2. APScheduler 内の pending job を全 pause
  3. 直近 N 時間(デフォルト 24h)に投稿した動画一覧を表示
  4. 確認プロンプト(対話) → YouTube Data API で privacyStatus=private に一括変更
  5. 操作内容を audit log テーブルに記録
```

このスクリプトは「コードロールバック」とは別建てで、 デプロイ前後を問わず使えるインシデント対応ツール。

## 結果

### 良い影響

- GPU 切り替え時の作業が「Dockerfile → push → env 変更」に圧縮される(設計上の保証)
- 平時は GPU driver / CUDA の整合トラブルを host 直で回避できる
- Makefile が「やれる操作」のリストになるため、 数ヶ月後の自分が迷わない
- reboot 後に scheduler が自動再開しないため、 想定外状態での自動投稿事故を防ぐ
- `make panic-stop` がコンプラ事故時の「殺し方」として最初から存在する
- DB マイグレーション前の自動 pg_dump で「うっかり alembic upgrade」事故から復旧可能

### 悪い影響・トレードオフ

- docker compose と systemd が両方走るため運用面のメンタルモデルが2つになる
- GPU worker を独立プロセスにすることで、 backend ↔ worker の HTTP 往復遅延がわずかに増える(数 ms オーダー、無視できる)
- reboot 後に scheduler が止まるため、 自分が気づくまで投稿が停止する(動機 B の継続性に対しては許容トレードオフ)
- down migration を書かないため、 後退が必要なときは手動 SQL を書く

### 受容したリスク

- `make deploy` 中断時の中途半端な状態(migration は走った、image は古い等)を完全には防げない。 healthcheck.sh で起動後の最低限の検知のみ
- GPU worker を後で containerize する場合、Dockerfile を「実際に build & run まで通したことがある」状態に維持する規律が必要。 CI で `docker build` だけは流す

## 検討した代替案

### 実行方式

- **全部 systemd:** PostgreSQL の pgvector セットアップが手間。 frontend の Node 管理も煩雑。不採用
- **全部 docker-compose(GPU 含む):** RunPod 移行は最短だが、日々の driver/cuda トラブルコストが継続。 1人運用には負担。不採用
- **frontend のみ Vercel:** 初期は public 公開 or VPN が必要、 オーバーキル。不採用

### デプロイ手順

- **GitHub Actions self-hosted runner:** Private repo + token 管理 + runner プロセス管理のコストに対し、 1人運用の利得が薄い。不採用
- **bare git hook (post-receive):** debug 時の事故が多く、ログが追いにくい。不採用
- **Ansible:** 1台では YAGNI。不採用

### オートスタート

- **全部オートスタート(scheduler 含む):** reboot 直後の不安定状態で自動投稿が走るリスク、 動機 C のコンプラ感応度的に不採用
- **全部手動:** reboot のたびに dryrun レビュー UI も使えないのは不便、不採用

### ロールバック

- **Alembic down migration 必須化:** 1人運用で down を「毎回正しく書く」のは現実的でない、 検証されない down は事故源。不採用
- **`pg_dump` restore のみ(自動 dump なし):** 「マイグレーション前にバックアップを取る」ことを忘れた時に詰む。不採用

## 関連

- ADR-0006: サイクル構造(日次+週次)
- ADR-0007: dryrun モード MVP 必須
- ADR-0009: FastAPI
- ADR-0010: PostgreSQL + pgvector
- ADR-0011: APScheduler in backend process
- ADR-0022: fsspec ストレージ抽象化
- ADR-0026: バックアップ方針(local secondary disk)
- ADR-0028: 5 種類のエラーカテゴリ
- ADR-0029: モノレポ構成
- ADR-0030: git 運用ポリシー
