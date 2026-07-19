"""StorageAdapter URI 閉じ込め (is_under_base) の単体テスト。"""

from __future__ import annotations

import pytest

from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

pytestmark = pytest.mark.unit


def test_is_under_base_accepts_child_uri() -> None:
    storage = StorageAdapter(base_uri="file:///srv/ymg/outputs")
    assert storage.is_under_base("file:///srv/ymg/outputs/music/a.wav") is True
    assert storage.is_under_base("music/a.wav") is True


def test_is_under_base_rejects_outside_and_traversal() -> None:
    storage = StorageAdapter(base_uri="file:///srv/ymg/outputs")
    assert storage.is_under_base("file:///etc/passwd") is False
    assert storage.is_under_base("file:///srv/ymg/outputs/../etc/passwd") is False
    assert storage.is_under_base("file:///srv/ymg/outputs_evil/x.wav") is False


def test_is_under_base_false_when_base_unset() -> None:
    storage = StorageAdapter(base_uri=None)
    assert storage.is_under_base("file:///tmp/x.wav") is False
