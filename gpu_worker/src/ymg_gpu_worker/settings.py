"""worker のランタイム設定フラグ (環境変数の薄い解釈)。

ここでは torch / GPU を一切触らない軽量なフラグだけを扱う。 代表例が
``GPU_WORKER_DUMMY`` で、 ``1`` のとき GPU / torch 無しの決定論的なダミー
生成経路を有効化する (ADR-0031 の遅延 import 構造を壊さず、 ローカル開発 /
CI / E2E で実モデル不在でも succeeded を返せるようにする)。

設計方針:

- 真偽値の解釈は 1 か所に集約し、 ``"1" / "true" / "yes" / "on"`` を真とする
  (大文字小文字・前後空白は無視)。 それ以外 (未設定含む) は偽。
- 副作用なし。 呼び出しごとに環境変数を読むだけで、 内部状態を持たない。
"""

from __future__ import annotations

import os
from typing import Final

DUMMY_MODE_ENV: Final = "GPU_WORKER_DUMMY"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _env_truthy(value: str | None) -> bool:
    """環境変数値が真を表すか判定する (未設定 / 空文字は偽)。"""
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY


def dummy_mode_enabled() -> bool:
    """ダミー生成経路が有効か (``GPU_WORKER_DUMMY`` が真) を返す。"""
    return _env_truthy(os.environ.get(DUMMY_MODE_ENV))
