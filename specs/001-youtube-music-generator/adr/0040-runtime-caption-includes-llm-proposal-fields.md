# ADR-0040: 実行時プロンプトへ LLM提案フィールドを決定論連結する

- **ステータス:** Accepted
- **日付:** 2026-07-19
- **決定者:** @seita
- **タグ:** backend / ml

## 背景

music compiler の実行時プロンプト(`caption`)は当初 `genre + subtheme + instrumental + BPM + key` のみだった。
LLM提案には既に `instruments` / `arrangement` / `texture` があり、トラック差分化の主材料なのに caption へ載せていなかった。
実行時に追加の LLM 呼び出しで caption を厚くするのは、承認画面の preview と実生成の乖離を招く(ADR-0039)。

## 決定

compile 時に、既存の LLM提案フィールドが非空なら caption へ決定論的に連結する。

順序:

```text
genre, subtheme, [instruments], [arrangement], [texture], instrumental, {final_bpm} BPM, key {music_key}
```

制約:

- 実行時の新規 LLM 呼び出しはしない
- `visual_direction` は混ぜない
- `duration_sec` / `seed` / `model` の方針は変更しない
- `COMPILER_VERSION` を `music_compiler_v2` に上げ、旧 compilation hash を無効化する

## 結果

### 良い影響

- 承認前 preview と GPU payload の caption が、楽器・展開・質感まで一致する
- 6 トラックの差が seed 以外の文言としても明示される

### 悪い影響・トレードオフ

- 既存の `music_compilations` は再 compile しないと新 caption にならない
- caption が長くなり、モデル側のトークン/条件付け特性は運用で観察が必要

### 受容したリスク

- LLM提案文面の品質がそのまま caption 品質になる(検証は別ゲート)

## 未決事項

- BPM / key のトラック間分散(意図的なばらつき)は本 ADR の対象外
- caption 長上限や語彙正規化は必要になった時点で version bump する

## 検討した代替案

- **実行時に仕上げ LLM で caption 再合成:** preview と乖離するため不採用(ADR-0039)
- **instruments のみ追加:** arrangement / texture も差分化材料として既にあるため不採用

## 関連

- ADR-0032: 改善計画 LLM の出力スキーマ(LLM提案 / システム確定)
- ADR-0039: 根拠付き承認・仕様 hash・Audio QA
- `backend/src/ymg_backend/domain/pipeline/music_compiler.py`
