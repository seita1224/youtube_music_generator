"""ACE-Step ランナー (T057)。

ローダ + 1 リクエストごとの音楽生成 (prompt / duration_sec / bpm / seed)。

重要: torch / ACE-Step パイプラインは **生成関数の内部で遅延 import** する。
ローカル開発機に torch / ACE-Step 未インストールでも、 このモジュールの import
自体は成功する (ADR-0031)。

本実装は **ACE-Step 1.5** (``ace-step/ACE-Step-1.5``) の
``AceStepHandler`` + ``generate_music`` API に合わせる。
チェックポイントは ``YMG_MODELS_DIR/acestep``、 無ければ
``ACE_STEP_PROJECT_ROOT/checkpoints`` (既定: 隣接の ACE-Step-1.5 クローン) を使う。

``thinking=False`` で 5Hz LM をスキップし、 caption / lyrics / bpm を直接 DiT に渡す
(PoC / dryrun 検証を LLM 初期化なしで回せるようにする)。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any, Final

from ymg_gpu_worker.api.schemas import MusicGenerateRequest
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.jobs.queue import JobResult
from ymg_gpu_worker.runners.device import models_dir, peak_vram_mb

_MODEL_SUBDIR: Final = "acestep"
_MODEL_NAME: Final = "acestep-1.5"
_DEFAULT_LYRICS: Final = "[Instrumental]"
_DEFAULT_CONFIG: Final = "acestep-v15-turbo"
_PROJECT_ROOT_ENV: Final = "ACE_STEP_PROJECT_ROOT"
_DEFAULT_PROJECT_ROOT: Final = "/home/seita/Program/ACE-Step-1.5"


class AceStepRunner:
    """ACE-Step 1.5 ハンドラの薄いラッパ。

    ハンドラは初回 ``generate`` 時に遅延ロードし、 以後は再利用する。
    """

    __slots__ = ("_dit", "_project_root", "_storage")

    def __init__(self, storage: StorageAdapter) -> None:
        self._storage: Final[StorageAdapter] = storage
        self._dit: Any | None = None
        self._project_root: Path | None = None

    @property
    def model_name(self) -> str:
        """models_loaded 表示用のモデル識別子。"""
        return _MODEL_NAME

    @property
    def is_loaded(self) -> bool:
        """ハンドラがロード済みか。"""
        return self._dit is not None

    def _resolve_project_root(self) -> Path:
        """チェックポイントを含む ACE-Step プロジェクトルートを解決する。"""
        if self._project_root is not None:
            return self._project_root

        env_root = os.environ.get(_PROJECT_ROOT_ENV)
        candidates = [
            Path(env_root) if env_root else None,
            models_dir() / _MODEL_SUBDIR,
            Path(_DEFAULT_PROJECT_ROOT),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            # checkpoints/ 配下に turbo がある構成、 または root 自体が checkpoints
            if (candidate / "checkpoints" / _DEFAULT_CONFIG).is_dir():
                self._project_root = candidate
                return candidate
            if (candidate / _DEFAULT_CONFIG).is_dir() and (
                candidate / "vae"
            ).is_dir():
                # YMG_MODELS_DIR/acestep が checkpoints 相当の場合
                self._project_root = candidate.parent
                return self._project_root
        raise FileNotFoundError(
            "ACE-Step 1.5 checkpoints not found. "
            f"Set {_PROJECT_ROOT_ENV} or place weights under "
            f"{models_dir() / _MODEL_SUBDIR}"
        )

    def _load(self) -> Any:
        """AceStepHandler を遅延ロードする。"""
        if self._dit is not None:
            return self._dit

        from acestep.handler import AceStepHandler  # type: ignore[import-not-found]

        project_root = self._resolve_project_root()
        dit = AceStepHandler()
        dit.initialize_service(
            project_root=str(project_root),
            config_path=_DEFAULT_CONFIG,
            device="cuda",
        )
        self._dit = dit
        return dit

    def generate(self, request: MusicGenerateRequest) -> JobResult:
        """1 リクエスト分の音楽を生成し、 ``output_uri`` へ書き出す。"""
        from acestep.inference import (  # type: ignore[import-not-found]
            GenerationConfig,
            GenerationParams,
            generate_music,
        )

        dit = self._load()
        started = time.monotonic()

        params = GenerationParams(
            caption=request.prompt,
            lyrics=_DEFAULT_LYRICS
            if not request.negative_prompt
            else _DEFAULT_LYRICS,
            instrumental=True,
            bpm=request.bpm,
            keyscale=request.music_key or "",
            duration=float(request.duration_sec),
            seed=request.seed if request.seed is not None else -1,
            thinking=False,
            inference_steps=8,
        )
        config = GenerationConfig(
            batch_size=1,
            use_random_seed=request.seed is None,
            seeds=[request.seed] if request.seed is not None else None,
            audio_format="wav",
        )

        with tempfile.TemporaryDirectory(prefix="ymg-acestep-") as tmp:
            result = generate_music(
                dit,
                None,  # thinking=False のため LM 不要
                params,
                config,
                save_dir=tmp,
            )
            if not getattr(result, "success", False):
                raise RuntimeError(
                    f"ACE-Step generate failed: {getattr(result, 'error', result)}"
                )
            audio_path = _first_audio_path(result)
            audio_bytes = Path(audio_path).read_bytes()

        self._storage.write_bytes(request.output_uri, audio_bytes)

        duration_ms = int((time.monotonic() - started) * 1000)
        return JobResult(
            output_uri=request.output_uri,
            duration_ms=duration_ms,
            vram_peak_mb=peak_vram_mb(),
        )


def _first_audio_path(result: Any) -> str:
    """``GenerationResult.audios`` から最初の音声パスを取り出す。"""
    audios = getattr(result, "audios", None) or []
    for item in audios:
        if isinstance(item, dict) and item.get("path"):
            path = Path(item["path"])
            if path.is_file():
                return str(path)
        if isinstance(item, str) and Path(item).is_file():
            return item
    raise RuntimeError(f"ACE-Step produced no audio file (result={result!r})")
