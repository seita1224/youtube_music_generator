"""ストレージ抽象化(fsspec ラッパ、ADR-0022)。

``file:// / s3:// / gs://`` を URI で統一的に扱う薄いラッパを公開する。
"""

from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

__all__ = ["StorageAdapter"]
