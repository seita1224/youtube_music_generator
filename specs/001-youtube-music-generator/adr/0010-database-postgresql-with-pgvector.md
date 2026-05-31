# ADR-0010: DB = PostgreSQL(将来の pgvector / RAG 拡張を見据える)

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, infra, ml

## 背景

DB 選定。データ規模だけ見れば SQLite で十分(年間 10 万レコード未満、書込頻度は日/週単位、1人運用)。
しかし以下を考慮:

- 改善計画履歴・コメント分析・楽曲メタを **embedding して類似検索する RAG 拡張** を将来スコープに入れる
- pgvector のエコシステム(LangChain・LlamaIndex・各種 LLM フレームワーク統合)は SQLite 系より大幅に成熟
- JSONB による構造化データ保存(改善計画 v_n の LLM 出力)も PostgreSQL のが表現力豊か
- 全文検索(コメント分析)も pg_trgm / tsvector が利用可能
- 1人運用での運用負荷増は docker-compose で吸収可能、難易度差は小さい(SQLAlchemy 利用なら実装上の差はほぼなし)

## 決定

- **PostgreSQL** を採用
- `pgvector` 拡張を初期から導入(embedding カラムを必要に応じて追加できる準備のみ。MVP では未使用)
- ローカル運用は **docker-compose** で立ち上げ
- ORM は **SQLAlchemy 2.x + Alembic**(マイグレーション)
- バックアップは `pg_dump` の日次スケジュール
- 将来の RAG 拡張時に embedding カラム(`vector`)を Alembic マイグレーションで追加可能

## 結果

### 良い影響

- 将来の RAG 拡張で DB 移行不要、手戻りゼロ
- JSONB・全文検索・拡張機能が豊富で、改善計画やコメント分析の構造化保存に強い
- LangChain・LlamaIndex 系の `PGVector` ベクタストアにそのまま乗せられる
- ORM(SQLAlchemy)経由なら開発体験は SQLite と差がほぼない

### 悪い影響・トレードオフ

- docker-compose で PostgreSQL を立てる運用が必要(SQLite の `cp` バックアップに比べると一手間)
  - 緩和: Makefile or `docker compose up -d db` で1コマンド起動
- バックアップ運用が必要(`pg_dump` + retention)
  - 緩和: cron + 圧縮ローテーション

### 受容したリスク

- 現在の規模(1人・日数本)に対しては機能過剰
  - 将来 RAG・規模拡大の保険として受容

## 検討した代替案

- **SQLite:** 規模的には十分。RAG 拡張時に sqlite-vec も存在するが、エコシステムが薄い。将来 PostgreSQL 移行コストを払うなら最初から PostgreSQL で開始する方が手戻りが少ない。不採用。
- **SQLite で開始 → 必要なら PostgreSQL 移行:** 移行コストはスキーマ書き換え + データ移送で軽くはない。RAG 拡張を現実的なロードマップに入れるなら、最初から PostgreSQL の方が合理的。不採用。
- **DuckDB:** 分析クエリに強いが、トランザクショナル用途には向かず、別系統 DB を立てるオーバーキル。不採用。

## 関連

- ADR-0001: システム構成
- ADR-0006: サイクル構造(改善計画 v_n の保存先)
- ../requirements.md §状態管理, §データ永続化
- pgvector: <https://github.com/pgvector/pgvector>
