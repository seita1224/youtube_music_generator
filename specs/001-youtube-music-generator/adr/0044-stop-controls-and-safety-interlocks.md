# ADR-0044: 停止手段の再定義(2 段階のシステム状態)と安全装置

- **ステータス:** Accepted
- **日付:** 2026-07-25
- **決定者:** @seita
- **タグ:** ops / backend / policy

## 背景

旧設計の停止手段は次の 3 つに分散していた。

- `make panic-stop`(ADR-0031): 投稿停止 + 該当動画の private 化。Constitution I が「常備」を要求する
- scheduler の手動 enable / disable(ADR-0011、ADR-0031): 再起動のたびに手動 enable
- dryrun モードの ON / OFF(ADR-0007)

ADR-0042 で後ろ 2 つを廃止したため、停止手段は `make panic-stop` だけになる。しかし「CLI で止める」は、止めたい典型ケース(自動公開が想定外の動きをしている)でユーザーが端末の前にいるとは限らないという問題を持つ。加えて ADR-0043 の L1 / L2 は「異常があれば通知が来る」ことを前提にしているが、**システム自体が沈黙して止まった場合を検知する仕組みが無い**。

## 決定

### (1) 停止を 2 段階のシステム状態として定義する

停止を UI ボタンや Makefile ターゲットではなく、**永続化されたシステム状態**として定義する。`system_state` テーブルに 1 行だけ持つ。

| 状態 | 意味 |
|---|---|
| `running` | 通常運転 |
| `publish_paused` | **公開だけ停止**。企画・生成・検査・パッケージングは継続する |
| `stopped` | 全ワーカー停止。進行中の工程は完了させ、新しい工程は開始しない |

**参照点を 2 箇所に固定する**。ここを通らない実行経路を作ってはならない。

1. 各工程を開始する直前(`stopped` なら開始しない)
2. YouTube への公開 API を呼び出す直前(`publish_paused` / `stopped` なら公開しない)

`publish_paused` 中の枠は `approved` のまま待機し、`publish_block_reason = system_paused` を持つ(ADR-0045)。解除後に遅延公開として処理される。

### (2) 操作チャネルは CLI + Slack

| チャネル | コマンド |
|---|---|
| CLI | `ymg stop` / `ymg pause-publishing` / `ymg resume` |
| Slack | 同等のコマンドまたはボタン |

admin UI には停止操作を置かない。UI は LAN 内からしか触れず、緊急時に最も届きにくいチャネルであるため。

### (3) compliance 事象では自動で止める

次の事象を検知した場合、人の操作を待たずに自動で `publish_paused` + **L0 への降格** + Slack 通知を行う。

- 公開後に指紋一致が判明した場合(該当動画は private 化する。ADR-0005 / 旧 `make panic-stop` の動作を継承)
- YouTube からポリシー違反・著作権の通知を受けた場合
- `containsSyntheticMedia` 未設定の投稿を検知した場合(ADR-0020)

エラー分類(ADR-0028)との対応は次のとおり。5 分類そのものは継承する。

| カテゴリ | 新モデルでの挙動 |
|---|---|
| `transient` | 自動リトライ(max 2)。枠の状態は変えない |
| `recoverable` | パラメータ調整 + リトライ。上限超過で枠を `failed` へ |
| `fatal` | `stopped` + Slack 通知 |
| `compliance` | `publish_paused` + L0 降格 + 該当動画 private 化 + Slack 通知 |
| `quality` | 該当トラックの自動再生成。上限超過で枠を `failed` へ |

### (4) 死活監視(heartbeat)

スケジューラは毎朝 **07:30 JST** に当日サマリ(今日の公開予定枠・要確認件数・システム状態)を Slack に送信する。

- **サマリが届かない日はシステムが停止していると判断できる**。これを唯一の死活シグナルとする
- L1 / L2 は「異常時に通知が来る」ことを前提とするため、**heartbeat の運用は L1 以上の前提条件**とする

### (5) Constitution I の改版

Principle I の「コンプラ事故時の即時停止手段(`make panic-stop`)を運用ツールとして常備する」を、次に置き換える。

> 停止手段(CLI + Slack から到達できる `publish_paused` / `stopped`)を常備し、compliance 事象では人の操作を待たず自動で `publish_paused` + 該当動画 private 化 + L0 降格を行う。

Critical path(Principle II)の `make panic-stop` の YouTube private 化は、**「compliance 自動停止経路(状態遷移 + private 化)」**に読み替える。

## 結果

### 良い影響

- 「押しに行く停止」から「勝手に止まる停止」に変わり、人が気づく前に被害が止まる
- 状態が DB の 1 行に集約され、「今止まっているのか」が全画面・全ワーカーで一致する
- 公開だけ止めて制作は続けられるため、原因調査中に生成が無駄にならない

### 悪い影響・トレードオフ

- 停止操作が UI に無いため、UI しか触っていないときは「どこから止めるのか」が自明でない。設定画面に現在のシステム状態と停止コマンドを**表示だけ**する(操作はしない)
- Slack 依存が増える。Slack が落ちている間は CLI のみになる

### 受容したリスク

- heartbeat は「人が Slack を見ている」ことに依存する。見落とせば停止に気づかない。個人運用の範囲としてこのリスクを受容する(外形監視サービスの導入は将来課題)
- 進行中の工程は `stopped` でも完走させるため、真に即時ではない。GPU 実行中のプロセス強制終了は行わない

## 検討した代替案

- **UI に緊急停止ボタンを置く:** ユーザーが UI 常時監視をしないため実効性が低い。「常備」の形式要件を満たすだけの飾りになる。不採用
- **`stopped` の 1 段階だけにする:** 公開だけ止めたい場面(調査中)で制作まで止まり、復帰に時間がかかる。2 段階を採用
- **compliance でも人の確認を待つ:** 待っている間に被害が広がる。自動停止を採用
- **外形監視サービス(healthchecks.io 等)を使う:** 依存が増える。まず Slack の daily heartbeat で始める

## 関連

- ADR-0043: L0 自動降格、heartbeat が L1 以上の前提であること
- ADR-0045: `publish_block_reason` と `failed` 状態
- ADR-0028: エラー 5 分類(継承)
- ADR-0005 / ADR-0020: compliance 判定の根拠
- 憲法改版: Principle I(panic-stop 常備 → 停止状態の常備 + 自動停止)、Principle II(critical path の読み替え)
- supersedes: ADR-0031 の `make panic-stop` と scheduler 手動 enable の部分
