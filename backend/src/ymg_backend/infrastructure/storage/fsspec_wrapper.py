"""fsspec を用いたストレージ抽象化の薄いラッパ(ADR-0022)。

``file:// / s3:// / gs://`` を URI で統一的に扱う。fsspec の
``AbstractFileSystem`` を内包し、protocol に応じたバックエンドへ委譲する。

設計方針:

- protocol を URI から都度解決するため、複数バックエンドを 1 インスタンスで扱える。
- ``base_uri`` を渡すと、protocol を持たない相対パスをその基点配下に解決する。
  protocol 付きの絶対 URI は base を無視してそのまま使う。
- 不変性: アダプタは設定(``base_uri`` / ``storage_options``)を保持するだけで、
  内部状態を書き換えない。各操作は呼び出しごとに fs を解決する。
"""

from __future__ import annotations

import os
from typing import IO, Any, Final

from fsspec.core import url_to_fs  # type: ignore[import-untyped]

_STORAGE_BASE_URI_ENV: Final = "STORAGE_BASE_URI"
_PROTOCOL_SEP: Final = "://"


def _has_protocol(uri: str) -> bool:
    """URI が ``scheme://`` 形式の protocol を明示しているか判定する。"""
    return _PROTOCOL_SEP in uri


class StorageAdapter:
    """fsspec ベースのストレージアダプタ(薄いラッパ)。

    Args:
        base_uri: 相対パス解決の基点となる URI(例: ``file:///var/yt-music/``)。
            ``None`` の場合は環境変数 ``STORAGE_BASE_URI`` を参照し、それも
            無ければ base 無し(渡された URI をそのまま使う)。
        storage_options: fsspec バックエンドへ渡す追加オプション(例: S3 の
            ``key`` / ``secret`` / ``endpoint_url`` 等)。

    Raises:
        ValueError: ``base_uri`` に protocol が含まれていない場合。
    """

    __slots__ = ("_base_uri", "_storage_options")

    def __init__(
        self,
        base_uri: str | None = None,
        *,
        storage_options: dict[str, Any] | None = None,
    ) -> None:
        resolved_base = base_uri if base_uri is not None else os.environ.get(_STORAGE_BASE_URI_ENV)
        if resolved_base is not None:
            resolved_base = resolved_base.strip()
            if not resolved_base:
                resolved_base = None
            elif not _has_protocol(resolved_base):
                raise ValueError(
                    f"base_uri must include a protocol (e.g. file://): {resolved_base!r}"
                )
        self._base_uri: Final[str | None] = (
            resolved_base.rstrip("/") + "/" if resolved_base is not None else None
        )
        self._storage_options: Final[dict[str, Any]] = dict(storage_options or {})

    @property
    def base_uri(self) -> str | None:
        """設定済みの基点 URI(末尾スラッシュ付き)。未設定なら ``None``。"""
        return self._base_uri

    def resolve_uri(self, path_or_uri: str) -> str:
        """相対パスを ``base_uri`` 配下の絶対 URI へ解決する。

        protocol 付きの URI はそのまま返す。base が未設定で protocol も無い
        場合は入力をそのまま返す(fsspec 既定の解釈に委ねる)。
        """
        if _has_protocol(path_or_uri):
            return path_or_uri
        if self._base_uri is None:
            return path_or_uri
        return self._base_uri + path_or_uri.lstrip("/")

    @staticmethod
    def _normalize_uri(uri: str) -> str:
        """``..`` 等を畳み、比較用に正規化した URI 文字列を返す。"""
        if not _has_protocol(uri):
            return os.path.normpath(uri)
        scheme, _, rest = uri.partition(_PROTOCOL_SEP)
        # ``file:///path`` → rest は ``/path``。 normpath は先頭スラッシュを保つ。
        return f"{scheme}{_PROTOCOL_SEP}{os.path.normpath(rest)}"

    def is_under_base(self, path_or_uri: str) -> bool:
        """解決後 URI が設定済み ``base_uri`` 配下に収まるか判定する。

        ``base_uri`` 未設定時は ``False`` (配信 API は拒否する)。
        ``file:///base/../etc/passwd`` のような traversal も正規化して弾く。
        """
        if self._base_uri is None:
            return False
        resolved = self.resolve_uri(path_or_uri)
        norm_resolved = self._normalize_uri(resolved)
        norm_base = self._normalize_uri(self._base_uri.rstrip("/")) + "/"
        if norm_resolved == norm_base.rstrip("/"):
            return True
        return norm_resolved.startswith(norm_base)

    def _fs_and_path(self, path_or_uri: str) -> tuple[Any, str]:
        """URI を解決し、fsspec の ``(filesystem, path)`` を返す。"""
        uri = self.resolve_uri(path_or_uri)
        fs, path = url_to_fs(uri, **self._storage_options)
        return fs, path

    def read_bytes(self, path_or_uri: str) -> bytes:
        """URI が指すオブジェクトの内容全体を bytes として読み出す。"""
        fs, path = self._fs_and_path(path_or_uri)
        data: bytes = fs.cat_file(path)
        return data

    def write_bytes(self, path_or_uri: str, data: bytes) -> None:
        """bytes を URI へ書き込む。親ディレクトリは自動生成する。"""
        fs, path = self._fs_and_path(path_or_uri)
        parent = fs._parent(path)
        if parent:
            fs.makedirs(parent, exist_ok=True)
        fs.pipe_file(path, data)

    def exists(self, path_or_uri: str) -> bool:
        """URI が指すオブジェクトが存在するか判定する。"""
        fs, path = self._fs_and_path(path_or_uri)
        result: bool = fs.exists(path)
        return result

    def delete(self, path_or_uri: str, *, missing_ok: bool = False) -> None:
        """URI が指すオブジェクトを削除する。

        Args:
            missing_ok: ``True`` の場合、対象が存在しなくても例外を送出しない。
        """
        fs, path = self._fs_and_path(path_or_uri)
        if missing_ok and not fs.exists(path):
            return
        fs.rm_file(path)

    def open(self, path_or_uri: str, mode: str = "rb", **kwargs: Any) -> IO[Any]:
        """URI に対するファイルライクオブジェクトを開く。

        書き込みモード(``w`` 系)では親ディレクトリを自動生成する。
        呼び出し側がコンテキストマネージャ等でクローズすること。
        """
        fs, path = self._fs_and_path(path_or_uri)
        if "w" in mode or "a" in mode or "x" in mode:
            parent = fs._parent(path)
            if parent:
                fs.makedirs(parent, exist_ok=True)
        handle: IO[Any] = fs.open(path, mode=mode, **kwargs)
        return handle
