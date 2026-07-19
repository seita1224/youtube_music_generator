# ADR-0005: Content ID 事前チェック = AcoustID + Chromaprint

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** ml, policy, ops

## 背景

当初要件は「Content ID 誤マッチも含めて LLM 判断 + 事後手動対処」だったが、グリル(Q4・Q5)で以下が判明:

- LLM は YouTube Content ID DB(契約パートナーのみアクセス)に **原理的に照会不能**。判断材料ゼロのため「LLM 判断」は実質ランダム
- 完全な事前 Content ID チェック手段は無料・有料問わず外部からは存在しない(Google が公開していない)
- 無料の **AcoustID + Chromaprint**(MusicBrainz の指紋DB照合、Apache 2.0)で、商用リリース楽曲との類似は実質 7〜8 割カバー可能
- 有料の ACRCloud は UGC/リミックスもカバー(月数十ドル〜)
- 1日1〜2本(ADR-0004)規模では事後対処件数は AcoustID で大幅に削減可能

## 決定

- **生成直後・投稿前**に AcoustID + Chromaprint で **指紋プレチェック** を必ず実行
- マッチ閾値を超えた楽曲は **その場で破棄して再生成**
- 再生成 N 回で連続マッチした場合は人間判断(Slack 通知 + 該当ジャンルをスキップ)
- LLM の責務からは **Content ID 判断を完全に外す**(ADR-0008)
- LLM 側には別途プロンプトポリシーを渡す:
  - 特定アーティスト名禁止
  - 現代のヒット曲名禁止
  - 固有名詞(楽曲タイトル・アーティスト名)禁止
- ACRCloud 等の有料サービスは Phase 2 として interface だけ用意し、当面は AcoustID のみで運用
- 投稿後に Content ID マッチが発生した場合の事後対処は手動(../requirements.md §運用)

## 結果

### 良い影響

- 投稿前に 7〜8 割の Content ID マッチを事前回避できる
- 無料(API キー不要、self-hosted も可能)
- 失敗時の責務が明確(LLM ではなく指紋認識パイプライン)

### 悪い影響・トレードオフ

- AcoustID DB に未登録の UGC・インディーズ独自登録・最新リリースは漏れる
  - 緩和: 漏れたものは事後対処(申立て対応 or 動画削除)
- AcoustID API はレート制限あり(self-host で回避可能)
- 指紋生成と照会で 1曲あたり数秒〜数十秒のオーバーヘッド

### 受容したリスク

- AcoustID で漏れた Content ID マッチは事後対処コストが残る(週 N 件超えたら自動停止判断ロジックを追加検討)

## 検討した代替案

- **完全な事前 Content ID チェック:** 手段が存在しない。不採用(原理不可能)。
- **ACRCloud で MVP 構築:** 月額コスト発生、当面の規模(1日1〜2本)では過剰。Phase 2 で評価。
- **事前チェックなし、事後対処のみ:** 事後対処件数が単調増加、規模拡大時に破綻。不採用。
- **LLM に Content ID 判断委任:** 原理的に判断材料がない。不採用。

## 関連

- ADR-0004: 投稿規模
- ADR-0008: LLM の責務範囲
- ../requirements.md §著作権方針
- AcoustID: <https://acoustid.org/>
- Chromaprint: <https://github.com/acoustid/chromaprint>
- pyacoustid: <https://pypi.org/project/pyacoustid/>
