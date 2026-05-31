# ADR-0019: LLM Provider 実装方針 — OpenAI(OAuth + API key)/ Anthropic(API key)/ Ollama

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, ml, ops

## 背景

ADR-0002 で LLMProvider 抽象化層を作る方針が確定。
具体的な provider 実装と認証方式の方針を決める必要がある。

事実関係(2026-05 時点):

- **OpenAI**: Codex は ChatGPT Plus/Pro/Business/Enterprise に同梱され、OAuth フローでサブスクから API 消費可能。サードパーティ実装(`numman-ali/opencode-openai-codex-auth` 等)も存在。OAuth は本来 "personal use" 範囲を想定しており、自動投稿によるマネタイズ用途では ToS 解釈に注意が必要
- **Anthropic**: 2026-02-19 で Agent SDK でのサブスクトークン利用が **公式に禁止**。API key 認証必須
- **Ollama**: ローカル LLM、金銭コスト 0、VRAM コスト発生

ユーザー希望:

- OpenAI / Anthropic は **公式 SDK** で利用
- OpenAI は **Codex OAuth ルートも選択肢として持つ**(サブスク内で消費)
- 並行して **API key 認証も選択肢として残す**(従量課金、ToS クリア)
- **Ollama も外さない**(ローカル運用の余地を維持)

## 決定

LLMProvider 抽象化層に **3 つの provider 実装**を持ち、各 provider で複数の認証方式を選択可能にする。

### 1. OpenAIProvider

- **公式 SDK** = `openai` Python パッケージ
- **認証方式を2つサポート**(設定で選択):
  - `auth_mode = "api_key"`: 環境変数 `OPENAI_API_KEY`、API 従量課金、ToS クリア、デフォルト推奨
  - `auth_mode = "codex_oauth"`: Codex CLI と同様の OAuth フローでアクセストークンを取得し、`OpenAI(api_key=oauth_token, base_url=...)` で初期化。ChatGPT サブスクから消費
- Codex OAuth 実装は `numman-ali/opencode-openai-codex-auth` 等を参考にする独自実装(コードは取り込まずロジックを参考)
- 構造化出力: `response_format={"type": "json_schema", ...}`(ADR-0018)

### 2. AnthropicProvider

- **公式 SDK** = `anthropic` Python パッケージ
- **認証方式は API key のみ**:
  - `auth_mode = "api_key"`: 環境変数 `ANTHROPIC_API_KEY`、API 従量課金
- 将来 Anthropic が SDK のサブスク利用を解禁した場合のため、認証層は拡張可能な構造にしておく(現時点では実装しない)
- 構造化出力: `tools` パラメータで `input_schema` を渡す方式(ADR-0018)

### 3. OllamaProvider

- **公式 SDK** = `ollama` Python パッケージ
- ローカル LLM をバックエンドに使用
- VRAM 制約(ACE-Step + SDXL と共存)のため、**軽量モデル**(Qwen 2.5 3B / Llama 3.2 3B / Gemma 2 2B 等、VRAM 2〜4GB)を推奨
- 構造化出力: `format="json"` + Pydantic バリデーション(ADR-0018)

### Provider / 認証方式の選択

- 環境変数 `LLM_PROVIDER`(`openai` / `anthropic` / `ollama`)で選択
- 環境変数 `LLM_AUTH_MODE`(`api_key` / `codex_oauth`)で認証方式を選択(provider が対応している場合のみ)
- 管理UI からも切替可能(再起動なし、次回ジョブから反映)

### デフォルト初期値(推奨設定)

- **MVP 初期**: `openai` + `api_key`(月 ¥30〜¥200 程度、ToS クリア、シンプル)
- 動作確認後、コスト削減したくなれば: `openai` + `codex_oauth`(personal use 範囲を確認した上で)
- 完全コスト 0 にしたければ: `ollama`(品質トレードオフあり)

## 結果

### 良い影響

- 3 provider + 複数認証方式で運用フェーズに応じた柔軟な切替が可能
- 公式 SDK 利用で破壊的変更追従が容易
- ToS クリアな選択肢(API key)を常にデフォルトとして残せる
- Ollama 経路を維持することで、コスト 0 / オフライン運用 / 実験用途にも対応

### 悪い影響・トレードオフ

- 3 provider × 複数認証の実装・テスト・保守コスト
  - 緩和: 共通インターフェース(ADR-0002)を薄く保ち、各 provider の差分を局所化
- Codex OAuth は ToS 解釈にグレーゾーンが残る
  - 緩和: デフォルトは API key、OAuth は明示的な選択時のみ有効化、利用規約変更の追従責任を運用ルールに含める
- Ollama は VRAM(ACE-Step + SDXL と共存)・品質のトレードオフ
  - 緩和: 軽量モデル選定、品質要求が高い処理(改善計画立案)は API へ振り分け

### 受容したリスク

- **Codex OAuth のマネタイズ用途利用は OpenAI ToS グレー**
  - 対策: 初期は API key 利用、Codex OAuth は dryrun / 個人実験範囲で動作検証してから判断
  - 万一 OpenAI が制限・ブロックした場合は API key にフォールバック
- Anthropic のサブスク経由利用は現時点で不可、API key のみ
- Ollama は構造化出力の強制力が弱い(モデル依存)

## 検討した代替案

- **API 従量のみ:** シンプルでクリアだが、Codex OAuth・Ollama という選択肢を失う。柔軟性のために不採用。
- **Codex OAuth のみ:** ToS リスクを単一経路に集中、API key フォールバックがないと運用継続性が脆い。不採用。
- **Ollama のみ:** VRAM 競合・品質課題で MVP には弱い。将来オプションとして維持。

## 関連

- ADR-0002: LLMProvider 抽象化層
- ADR-0008: LLM の責務範囲
- ADR-0018: 構造化出力スキーマ
- 参考: <https://github.com/numman-ali/opencode-openai-codex-auth>
- 参考: <https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan>
- 参考: <https://code.claude.com/docs/en/authentication>
