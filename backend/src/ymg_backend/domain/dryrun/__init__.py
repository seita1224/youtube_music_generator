"""dryrun レビュー (US2) ドメインパッケージ。

DryrunService (承認→投稿 / 却下→削除+理由フィードバック / 7日auto_expire) と
動画配信ロジックをまとめる (T093 以降)。 ORM は
``ymg_backend.infrastructure.db.models`` の DryrunOutput / Post を使う。
"""
