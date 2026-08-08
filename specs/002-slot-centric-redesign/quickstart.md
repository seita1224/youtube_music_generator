# Quickstart: 枠中心モデルへの再設計

001 の quickstart(§0〜§10: OS パッケージ / モデル重み / OAuth / systemd / docker compose)は変更なく適用される。本書は**再設計で変わる部分だけ**を記す。

## 1. `.env` の差分

001 の `.env`(quickstart §3)をベースに、次を追加 / 削除する。

```bash
# --- 追加 ---
PUBLISH_WARN_PER_DAY=6                # 編成表の 1 日公開予定の警告閾値(research.md R-1。保守的初期値)
YMG_ADMIN_TOKEN=__set_strong_value__  # CLI / Slack 受信系が system API を呼ぶための管理トークン
SLACK_BOT_TOKEN=xoxb-...              # ボタン付き通知(chat.postMessage + Block Kit)
SLACK_APP_TOKEN=xapp-...              # Socket Mode(connections:write scope、research.md R-2)
LEGACY_DATABASE_URL=                  # ymg migrate-legacy 実行時のみ設定(読み取り専用)

# --- 削除(概念廃止) ---
# DRYRUN_DEFAULT=...                  # dryrun 概念の廃止(ADR-0042)
```

`SLACK_WEBHOOK_URL` は単純通知用に残す。

## 2. DB 初期化(新スキーマ)

```bash
# 新 DB を作成(旧 DB は凍結アーカイブとして残す。ADR-0050)
make migrate          # alembic 新チェーン 0001_slot_baseline を適用
                      # seed: genres 6 件 / system_state=running / autonomy_state=l0 / model_pricing
```

旧 DB からの実績移行(任意。目的関数が過去実績を使えるようになる):

```bash
LEGACY_DATABASE_URL=postgresql://... uv run ymg migrate-legacy
# 移行対象: videos(公開済みのみ)/ analytics_daily / genres / model_pricing / oauth_credentials
# (research.md R-4。1 回きり、恒久互換層なし)
```

## 3. Slack アプリの準備(ボタン操作用)

1. api.slack.com/apps で app を作成、Socket Mode を有効化
2. App-Level Token(`connections:write`)→ `SLACK_APP_TOKEN`
3. Bot Token(`chat:write`)→ `SLACK_BOT_TOKEN`、workspace にインストール
4. backend 起動時に Socket Mode 常駐タスクが接続する(公開エンドポイント不要)

## 4. 起動と最初の枠

```bash
make up               # backend + frontend + postgres
sudo systemctl start ymg-gpu-worker
```

1. 管理 UI(`http://<host>:3000`)にログイン → **編成表**(ホーム)
2. 公開枠パターンに枠行を 1 本追加(例: 明日 07:00 / ジャンル固定 Lo-Fi)→ 保存
3. 物化は毎日 03:00 JST。待たずに試す場合は編成表から「単発枠を追加」(明日の日時を指定)
4. 枠が `empty` → 制作開始(前日 03:00 の枠駆動生成、または枠詳細から手動開始)
5. `in_production` の間、枠詳細で各工程の入出力が見える(planning → generating → inspecting → packaging)
6. 完了で `awaiting_approval`(L0)。Slack に完成通知が届く
7. 枠詳細でプレビュー + 検査サマリを確認 → **承認** → `approved` → 公開予定時刻に自動公開

## 5. 停止・再開のドリル(運用前に 1 回試す)

```bash
uv run ymg pause-publishing   # 公開だけ停止(制作は続く)
uv run ymg resume             # 再開(待機分は遅延公開)
uv run ymg stop               # 全停止(進行中工程は完走、新規開始なし)
```

- Slack からも同等操作ができることを確認する
- 管理 UI の設定画面は**状態表示のみ**(操作は CLI / Slack。ADR-0044)
- 毎朝 07:30 JST の Slack サマリ(heartbeat)が届くことを確認する。**届かない日 = システム停止**のシグナル

## 6. 自動運転レベルの運用

- 既定 L0。設定画面に L1 / L2 の昇格条件と達成状況(例: 「あと 3 枠」)が表示される
- L0→L1: 直近 10 枠連続の無修正承認 + 検査自動不合格 0
- L1 の公開通知には取り下げボタンが付く(= YouTube private 化)
- 降格はいつでも即時。compliance 事象では自動で L0 に落ちる

## 7. 検証チェックリスト(運用開始前)

- [ ] 編成表 → 枠詳細 → 承認 → 公開 の一連が 1 枠通ること
- [ ] 却下(理由入力)→ `rejected_pending` → やり直し → 再承認 が通ること
- [ ] トラック 1 本だけ再生成 → 検査 / mix / video / package が作り直されること
- [ ] `ymg pause-publishing` 中に公開されないこと(`publish_block_reason=system_paused` 表示)
- [ ] compliance 自動停止のドリル(テストモードで指紋一致をシミュレート)
- [ ] heartbeat が届くこと
- [ ] `make backup` が永続保持対象のみをコピーすること(Fernet 鍵は対象外)

## 関連ドキュメント

- [spec.md](spec.md) / [plan.md](plan.md) / [data-model.md](data-model.md) / [research.md](research.md)
- contracts: [backend-api.yaml](contracts/backend-api.yaml)(新規)、[gpu-worker-api.yaml](contracts/gpu-worker-api.yaml)(001 継承・無変更)、[llm-provider-interface.md](contracts/llm-provider-interface.md)(同)
- 001 quickstart: `../001-youtube-music-generator/quickstart.md`(OS / モデル / OAuth / systemd)
