# ADR-0038: RequestValidationError の機密 input 除去

- **ステータス:** Accepted
- **日付:** 2026-07-13
- **決定者:** @seita
- **タグ:** backend / security

## 背景

FastAPI 既定の `RequestValidationError` ハンドラは Pydantic `errors()` をそのまま
422 応答の `detail` に載せる。 各要素の `input` に提出値が入るため、
`PUT /llm/credentials` で `api_key` が長すぎる・空白のみ・型不正のとき、
平文 API キーが HTTP 応答にエコーされる (セキュリティレビュー MEDIUM)。

加えて、 `loc` が機密フィールド名を含まない経路でも平文が残りうる:

- JSON 配列 / スカラー body → `loc=["body"]` のみで `input` に提出値全体
- 必須フィールド欠落 → 非機密 `loc` の `input` に body 全体 (秘密キー含む)
- `alias` / camelCase の `loc`、および `ctx` 内の秘密らしい mapping
- 配列インデックス末端 (`loc=("body", 0)` / `("body", "items", 0)`) の
  スカラー / list → 末端フィールド名が無く帰属できないのに平文が残る
- body ルート mapping の非文字列キー → 名前ヒューリスティックが使えず値が残る
- provider 固有・複数形の資格情報名 (`openai_key` / `jwt` / `api_keys` 等)

`loc` / `type` / `msg` はクライアント・運用双方に有用なので、 応答形式そのものは
崩さず、 提出秘密バイト列だけを応答・ログから除去する必要がある。

## 決定

1. `ymg_backend.core.validation_errors` に再利用可能なサニタイザと
   `RequestValidationError` ハンドラを置く。
2. エラー要素は常にディープコピーし、 入れ子の元参照を共有しない。
3. フィールド名は snake_case / camelCase / ハイフン / 連続区切りを正規化し、
   次を対象とする (完全一致・圧縮一致・接尾辞):
   - 基本: `api_key`・`password`・`secret`・`token`・`authorization_code`・
     `fernet_key`・`credentials` および別名 (`apiKey` / `apikey` / `clientSecret` 等)
   - 複数形: `api_keys`・`tokens` および接尾辞 `_api_keys` / `_tokens`
   - その他資格情報: `jwt` および接尾辞 `_jwt`
   - provider 固有: `openai_key`・`anthropic_key` および接尾辞
     `_openai_key` / `_anthropic_key` / 既存の `_api_key`
   - **含めない:** 広すぎる `*_key` 単独接尾辞 (`foreign_key` / `cache_key` 等の誤検知回避)
4. サニタイズ方針:
   - `loc` が機密フィールドを指す → `input` 全体をマーカー `***` に置換
   - 任意の `input` mapping / list → 秘密らしいキーの値を再帰的に伏せる
     (非機密 `loc` や `body` のみでも適用)
   - **フィールド帰属不可** (`loc` 末端が配列インデックス、または body/query 等の
     ルートのみ、または空) でスカラー / list / 不明型 → `input` 全体を伏せる
   - mapping の **非文字列・空キー** は名前帰属不可のため値を伏せる
     (body ルート mapping を含む)
   - `ctx` も同様に再帰サニタイズする
   - **明示帰属のある非機密スカラー** `input` と `type` / `loc` / `msg` は残す
5. `create_app` で当該ハンドラのみ登録し、 他の例外ハンドラ・認証・契約は変更しない。
6. ハンドラ内ログも `type` / `loc` / `msg` のみとし、 提出値は書かない。

## 結果

### 良い影響

- 422 でも API キー等の平文が応答・ログに出ない
- 配列 body・スカラー body・欠落フィールド・配列インデックス末端でも秘密が残らない
- body ルートの非文字列キー経路でも値が残らない
- 通常フィールドの検証エラーは従来どおりデバッグ可能

### 悪い影響・トレードオフ

- 機密フィールドの不正値そのものはクライアントに戻らない (意図的)
- フィールド帰属不能時 (body ルート・配列インデックス末端) のスカラー / list は
  非秘密でも `input` を伏せる (安全側)
- フィールド名ヒューリスティックに依存 (未知の別名は漏れうる)
- mapping 内の秘密キーは値のみ伏せ、 キー名の存在は残る (デバッグ用。 値は出さない)
- 非文字列キーの値は中身によらず伏せる (キー名で判定できないため)

### 受容したリスク

- 新シークレット用フィールド名を追加したとき、 命名が規約外だと赤化されない
  - 緩和: `is_sensitive_field_name` の明示リスト / 接尾辞 / 圧縮別名を拡張する
    (`*_key` 全般は誤検知が多いので採用しない)
- 配列インデックス末端で list / スカラー全体を伏せるため、 要素の非秘密デバッグ情報も失う
  - 緩和: 末端がフィールド名の通常経路 (`loc` が `window_hours` 等まで降りる) では
    従来どおり非機密スカラー `input` を残す

## 検討した代替案

- **フィールドごとに `HideInput` カスタムエラー:** スキーマ全体への侵襲が大きく不採用
- **422 の `detail` を文字列だけにする:** 通常フィールドの有用性を損なうため不採用
- **エンドポイント個別 try/except:** 漏れやすく、 再利用性がないため不採用
- **`loc` 一致のみの赤化:** 配列 / スカラー / 欠落フィールド経路で CRITICAL 漏洩が残るため不採用
- **広義 `*_key` 接尾辞:** `foreign_key` / `cache_key` 等の誤検知が多いため不採用。
  provider 固有と `_api_key` に限定

## 関連

- ADR-0009: FastAPI
- ADR-0013: 管理 UI 認証
- ADR-0023: 構造化ログ
- `PUT /llm/credentials` (`api/llm.py` / `LlmCredentialPutBody`)
