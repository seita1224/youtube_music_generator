"""SDXL 派生モデルランナー (T058)。

ローダ (default Juggernaut XL v10、 ADR-0016) + 画像生成。 モデルは設定で切替可能で、
切替時は VRAM 上のパイプラインを入れ替える。

重要: torch / diffusers は **生成関数の内部で遅延 import** する。 ローカル開発機に
torch / diffusers 未インストールでも、 このモジュールの import 自体は成功する
(ADR-0031)。 モデル重みは ``YMG_MODELS_DIR/sdxl/<model>`` に配置する前提
(ADR-0016, quickstart §5)。
"""

from __future__ import annotations

import time
from typing import Any, Final

from ymg_gpu_worker.api.schemas import ImageGenerateRequest
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.jobs.queue import JobResult
from ymg_gpu_worker.runners.device import models_dir, peak_vram_mb

_MODEL_SUBDIR: Final = "sdxl"
_DEFAULT_MODEL: Final = "juggernaut-xl-v10"

# ADR-0016: デフォルト + ジャンル別代替モデルの許可リスト。
# 値は ``YMG_MODELS_DIR/sdxl/<dir>`` 配下のディレクトリ名。
_KNOWN_MODELS: Final[dict[str, str]] = {
    "juggernaut-xl-v10": "juggernaut-xl-v10",
    "realvis-xl-v5": "realvis-xl-v5",
    "epicrealism-xl": "epicrealism-xl",
    "dreamshaper-xl": "dreamshaper-xl",
    "animagine-xl": "animagine-xl",
}


class SdxlRunner:
    """SDXL 派生モデルパイプラインのラッパ (モデル切替対応)。

    1 つのパイプラインを VRAM に常駐させ、 リクエストのモデルが現行と異なる場合のみ
    ロードし直す (ADR-0016: 同一ジャンルのバッチではモデル維持)。
    """

    __slots__ = ("_loaded_model", "_pipeline", "_storage")

    def __init__(self, storage: StorageAdapter) -> None:
        self._storage: Final[StorageAdapter] = storage
        self._pipeline: Any | None = None
        self._loaded_model: str | None = None

    @property
    def loaded_model(self) -> str | None:
        """現在ロード済みのモデル ID。 未ロードなら ``None``。"""
        return self._loaded_model

    @property
    def is_loaded(self) -> bool:
        """パイプラインがロード済みか。"""
        return self._pipeline is not None

    def _resolve_model(self, model: str) -> str:
        """リクエストのモデル ID を許可リストで検証して返す。"""
        if model not in _KNOWN_MODELS:
            raise ValueError(
                f"unknown SDXL model: {model!r} "
                f"(allowed: {', '.join(sorted(_KNOWN_MODELS))})"
            )
        return model

    def _load(self, model: str) -> Any:
        """指定モデルの SDXL パイプラインを遅延ロードする。

        torch / diffusers は関数内で import する (モジュール先頭では import しない)。
        現行と同一モデルがロード済みなら再利用する。
        """
        if self._pipeline is not None and self._loaded_model == model:
            return self._pipeline

        import torch  # 遅延 import
        from diffusers import StableDiffusionXLPipeline  # type: ignore[import-not-found]

        model_dir = str(models_dir() / _MODEL_SUBDIR / _KNOWN_MODELS[model])
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        pipeline = StableDiffusionXLPipeline.from_pretrained(
            model_dir,
            torch_dtype=dtype,
            use_safetensors=True,
        )
        if torch.cuda.is_available():
            pipeline = pipeline.to("cuda")
        self._pipeline = pipeline
        self._loaded_model = model
        return pipeline

    def generate(self, request: ImageGenerateRequest) -> JobResult:
        """1 リクエスト分の画像を生成し、 ``output_uri`` へ PNG を書き出す。"""
        import torch  # 遅延 import

        model = self._resolve_model(request.model)
        pipeline = self._load(model)
        started = time.monotonic()

        generator: Any | None = None
        if request.seed is not None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            generator = torch.Generator(device=device).manual_seed(request.seed)

        output = pipeline(
            prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            width=request.width,
            height=request.height,
            num_inference_steps=request.steps,
            guidance_scale=request.cfg_scale,
            generator=generator,
        )
        image = output.images[0]
        png_bytes = _encode_png(image)
        self._storage.write_bytes(request.output_uri, png_bytes)

        duration_ms = int((time.monotonic() - started) * 1000)
        return JobResult(
            output_uri=request.output_uri,
            duration_ms=duration_ms,
            vram_peak_mb=peak_vram_mb(),
        )


def _encode_png(image: Any) -> bytes:
    """PIL Image を PNG (bytes) へエンコードする。"""
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
