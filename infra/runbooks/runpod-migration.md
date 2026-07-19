# Runbook: GPU worker を RunPod へ移行する (T125)

> GPU worker (ACE-Step + SDXL) をローカル host 直 (systemd) からクラウド GPU (RunPod) へ
> 切り替える手順。 **ADR-0031 の設計保証により、 backend / frontend のコード変更は不要**。
> 切り替えは「Dockerfile build → push → Pod 起動 → `GPU_WORKER_BASE_URL` 差し替え → backend 再起動」に圧縮される。

## 0. なぜコード変更が不要か (ADR-0031 の要点)

backend ↔ GPU worker は **HTTP API 契約 + fsspec ストレージ** で疎結合している:

- backend は GPU worker を直接 import せず、 `GPU_WORKER_BASE_URL` 越しに HTTP で叩くだけ
  (`contracts/gpu-worker-api.yaml` の 4 エンドポイント: `GET /health` / `POST /generate/music` /
  `POST /generate/image` / `GET /jobs/{job_id}`)
- 入出力ファイルは fsspec URI (`file://` / `s3://` / `gs://`) で受け渡す (ADR-0022)
- worker の実行場所 (host か RunPod Pod か) は契約に現れない

したがって RunPod 側が **同じ契約を満たす限り**、 backend / frontend のソースは無変更で動く。
変えるのは「worker の置き場所を指す環境変数」と「(必要なら) 共有ストレージの URI」だけ。

参照: [`../../specs/001-youtube-music-generator/adr/0031-deploy-procedure.md`](../../specs/001-youtube-music-generator/adr/0031-deploy-procedure.md) §(1) /
[`../../specs/001-youtube-music-generator/contracts/gpu-worker-api.yaml`](../../specs/001-youtube-music-generator/contracts/gpu-worker-api.yaml)

## 1. 事前準備

- RunPod アカウント + 支払い設定
- コンテナレジストリ (Docker Hub / GHCR / RunPod の private registry いずれか)
- RunPod が `GPU_WORKER_BASE_URL` に到達できるネットワーク経路 (後述の proxy URL)
- worker の `Dockerfile` が repo に commit 済みであること (ADR-0031: 「build & run まで通したことがある」状態を維持)

## 2. GPU worker イメージを build & push

```bash
cd gpu_worker

# レジストリとタグ (例: GHCR)
export IMAGE=ghcr.io/seita1224/ymg-gpu-worker:$(git rev-parse --short HEAD)

docker build -t "$IMAGE" .
docker push "$IMAGE"
```

ポイント:

- worker は `127.0.0.1:8001` ではなく **`0.0.0.0:8001`** で listen させる
  (Pod 外からの到達が必要。 ローカル systemd では `127.0.0.1` に絞っていた点と異なる)
- ACE-Step / SDXL のモデル重みはイメージに焼くと巨大になるため、
  RunPod の **Network Volume** にダウンロードして起動時マウントするのが現実的

## 3. RunPod に Pod (または Serverless) をデプロイ

### Pod (常駐) の場合

1. RunPod console → **Pods → Deploy** → GPU (例: RTX 4090 / A5000 24GB 以上)
2. Container Image に `$IMAGE` を指定
3. **Expose HTTP Port** = `8001` (worker のポート)
4. Network Volume をモデル重み用にアタッチ (`/srv/ymg/models` 等にマウント)
5. 環境変数: backend と同じ `FERNET_KEY` は **不要** (worker は鍵を扱わない)。
   必要なのは fsspec ストレージのクレデンシャル (S3/R2 を使う場合のみ)
6. Deploy 後、 Pod の **proxy URL** を控える:
   `https://<podid>-8001.proxy.runpod.net`

### Serverless の場合

- Endpoint を作成し、 同イメージを指定。 ただし Serverless は cold start とリクエスト/レスポンス
  形態が Pod と異なるため、 worker 側を Serverless ハンドラ形式に合わせる改修が必要になる。
  まずは **Pod での疎通確認を先に行う** ことを推奨。

## 4. worker 単体の疎通確認 (backend を切り替える前)

```bash
# RunPod proxy URL に対して契約どおり叩けるか確認
curl -fsS https://<podid>-8001.proxy.runpod.net/health | jq
# 期待: {"status":"ok","gpu_available":true,"vram_free_mb":...,"models_loaded":[...]}
```

`/health` が 200 で `gpu_available: true` を返すまで、 backend は切り替えない。

## 5. 共有ストレージの移行 (必要な場合のみ)

ローカルでは `file:///srv/ymg/outputs/...` を backend と worker が同じディスクで共有していた。
worker がクラウドに出ると **同じローカルパスを共有できない** ため、 出力先を共有ストレージへ移す:

1. `.env` の `STORAGE_BASE_URI` を `s3://...` / `r2://...` 等へ変更 (ADR-0022)
2. backend / worker 双方に同じバケットのクレデンシャルを渡す
3. fsspec が URI スキームを見て自動でバックエンドを切り替える (コード変更なし)

> ローカル GPU を残しつつ RunPod を併用する移行期は、 共有を S3 互換に寄せておくと切り替えが楽。

## 6. backend を切り替える (差し替えは env 1 行)

```bash
# .env を編集
# GPU_WORKER_BASE_URL=http://127.0.0.1:8001
#   ↓
# GPU_WORKER_BASE_URL=https://<podid>-8001.proxy.runpod.net
$EDITOR .env

# backend を再起動して新しい URL を読み込ませる
make restart-backend          # = docker compose restart backend
```

backend のヘルスチェックで `gpu_worker: ok` を確認:

```bash
curl -fsS -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://127.0.0.1:8000/health | jq
```

## 7. ローカル GPU worker の停止 (移行完了後)

```bash
sudo systemctl disable --now ymg-gpu-worker
```

> 切り戻し (ロールバック) は逆順: `.env` の `GPU_WORKER_BASE_URL` をローカルに戻し、
> `sudo systemctl enable --now ymg-gpu-worker` → `make restart-backend`。

## チェックリスト

- [ ] `docker build` / `docker push` が通った (`gpu_worker/Dockerfile`)
- [ ] worker が `0.0.0.0:8001` で listen している
- [ ] RunPod proxy URL の `/health` が 200 / `gpu_available: true`
- [ ] (S3 移行時) `STORAGE_BASE_URI` を共有ストレージへ変更し、 両者に資格情報を付与
- [ ] `.env` の `GPU_WORKER_BASE_URL` を proxy URL に差し替え
- [ ] `make restart-backend` 後、 backend `/health` が `gpu_worker: ok`
- [ ] **backend / frontend のソースは 1 行も変更していない** (ADR-0031 の保証どおり)

## 関連

- ADR-0031 デプロイ手順 (GPU worker の切り替え可能性)
- ADR-0022 fsspec ストレージ抽象化
- `contracts/gpu-worker-api.yaml` (4 エンドポイント契約)
- `infra/systemd/ymg-gpu-worker.service` (ローカル host 直の常駐定義)
