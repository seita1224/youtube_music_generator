"""analytics ドメインパッケージ (US3)。

YouTube Data/Analytics API から動画別指標を取得して ``analytics_daily`` へ upsert する
取得層 (T101 analytics_client) / コメント取得 (T102 comments_client) / planner LLM 入力用と
frontend ``/analytics`` 用の集計ビュー (T103 summary) をまとめる。 ORM は
``ymg_backend.infrastructure.db.models`` の AnalyticsDaily / Comment / Video を使う。
"""
