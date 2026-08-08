"""テスト共通設定: 必須環境変数のテスト既定値。

``Settings`` は ``ADMIN_PASSWORD`` 空を起動時拒否する (ADR-0013)。 テストは開発者の
``.env`` に依存せず hermetic に走るべきなので、 未設定の場合のみダミー値を注入する
(``setdefault``。 CI / ローカルで明示設定されていればそちらが優先)。

個々のテストが検証する設定値 (Fernet 鍵の不正値など) は、 各テストが ``Settings``
を明示構築して上書きするためここの既定値に影響されない。
"""

from __future__ import annotations

import os

_TEST_ENV_DEFAULTS: dict[str, str] = {
    "ADMIN_PASSWORD": "test-only-admin-password",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "ymg",
    "POSTGRES_USER": "ymg",
    "POSTGRES_PASSWORD": "test-only-db-password",
}

for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
