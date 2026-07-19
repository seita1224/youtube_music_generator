"""MVP リリース判定 (T127, ADR-0035 §6 の 6 項目)。

ADR-0035 の MVP チェックリスト 6 項目を DB から判定し、 各項目を ``green`` / ``red`` で
返すドメイン層。 判定ロジック (checklist) と公開 I/F は後続 (T127) が
:mod:`ymg_backend.domain.mvp_check.checklist` 等に実装し、 ここで re-export する。
"""

__all__: list[str] = []
