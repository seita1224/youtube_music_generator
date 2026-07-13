# ADR-0037: LLM provider/model 不整合の GET 公開と PUT 修復

- **ステータス:** Accepted
- **日付:** 2026-07-13
- **決定者:** @seita
- **タグ:** backend, frontend, llm

## 背景

`app_state` に provider と model の不整合が残ると、 factory の strict 解決は `LlmError` になる。
一方 Settings UI は修復の入口であるべきで、 GET で不整合を隠し、 PUT が 500 になる状態は運用不能だった。

## 決定

1. **GET `/llm/providers`**: `resolve_provider_config(..., strict_model=False)` で永続値をそのまま返す。
2. **PUT `/llm/providers`**: リクエスト body は従来どおり厳格検証。 audit `from` 用の現行値読取だけ non-strict（`LlmError` 時は app_state 生値へフォールバック）。有効組合せへの修復は決定的に 200 を返す。
3. **Frontend**: hydrate 時に不整合 `active.model` を `models[0]` へ silent 置換しない。要修正バナーを出し、ユーザーが有効モデルを選んで保存する。明示的な provider 変更時のみ既定モデルへ切替える。

## 結果

- 不整合でも Settings から修復できる
- factory 実行経路は引き続き strict（修復後に解決可能）
- UI は不正組合せを隠蔽せず、保存は有効モデル選択後のみ
