# ADR-0009: バックエンド Web フレームワーク = FastAPI

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, infra

## 背景

ADR-0001 でバックエンド = Python に決定済み。具体的な Web フレームワーク選定が必要。
本システムの API 要件:

- REST(CRUD・設定操作)
- SSE(進捗・ログのリアルタイム配信)
- OpenAPI スキーマ自動生成(管理UI 側で `openapi-typescript` から TS 型を起こす)
- Pydantic v2 統合(LLM 出力の構造化バリデーション・設定の型安全)
- 内部 1 ユーザー向け、外部公開なし(高負荷・水平スケーリング要件なし)

## 決定

- **FastAPI** を採用
- SSE は `sse-starlette` を併用
- 起動は `uvicorn`(開発)、必要に応じて `gunicorn + uvicorn workers`(本番)
- OpenAPI スキーマは CI で生成、フロントエンドの型生成は別ジョブで連動

## 結果

### 良い影響

- Python における事実上のデファクトで情報量が多い
- Pydantic v2 ネイティブ統合により、LLM 出力スキーマ・API スキーマ・設定スキーマを単一の型で表現できる
- OpenAPI 自動生成によりフロントエンド型と整合性 CI が成立する
- SSE・依存性注入・バックグラウンドタスクが標準で揃う

### 悪い影響・トレードオフ

- ASGI 系の起動構成(uvicorn / gunicorn)を理解する必要がある(影響は小さい)
- GPU 推論のような長時間処理は `BackgroundTasks` に流すと API レスポンスとの分離設計が必要
  - 緩和: スケジューラ(ADR は別途)とは別に分けて設計

### 受容したリスク

- フレームワーク自体は安定だが、Pydantic v1 → v2 のような破壊的変更が将来発生する可能性
  - 対策: バージョン固定 + dependabot 系で計画的に追従

## 検討した代替案

- **Litestar:** 型に厳密で msgspec も使えるが、コミュニティ・エコシステムが FastAPI に比べて小さい。新しさのリスクを取るメリットが薄いため不採用。
- **Flask + 拡張:** SSE・OpenAPI・型統合が追加実装になり、生産性で劣る。不採用。
- **Starlette 直叩き:** 軽量だが OpenAPI 自動生成や型統合を手書きする必要があり、本プロジェクトの規模に対して過剰な低レベル制御。不採用。

## 関連

- ADR-0001: システム構成(Python + Next.js)
- ../requirements.md §システム構成
