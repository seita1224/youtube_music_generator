# GPU worker イメージ (ADR-0031)。 クラウド GPU (RunPod 等) 移行用に最初から用意。
# host 直 + systemd でも、 このイメージをそのまま流用できる契約。
# ベース: CUDA 12.4 runtime + cuDNN。 ACE-Step 1.5 / SDXL (Juggernaut XL v10) ランタイム前提。
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git ffmpeg libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# uv (Python 3.13 を uv 自身が用意する)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv python install 3.13 \
    && uv sync --no-dev --no-install-project

COPY . .
RUN uv sync --no-dev

# モデル重みはイメージに焼かず、 ボリューム/HF キャッシュ経由 (quickstart §5)
ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/models/hf \
    YMG_MODELS_DIR=/models

EXPOSE 8001
CMD ["uvicorn", "ymg_gpu_worker.main:app", "--host", "0.0.0.0", "--port", "8001"]
