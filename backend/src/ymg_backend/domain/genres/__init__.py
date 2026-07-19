"""genres ドメインパッケージ (US3)。

承認済み WeeklyPlan を ``genres`` テーブルへ反映する rotation (T105) / experiment_slot の
経過と retention 比から採用・削除を判定する recommend (T106) をまとめる。 ORM は
``ymg_backend.infrastructure.db.models`` の Genre / Plan / AnalyticsDaily を使い、
``api/plans.py:_load_enabled_genres`` が拾う ``Genre.enabled`` を更新する。
"""
