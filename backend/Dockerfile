# backend イメージ (ADR-0029)。 uv で依存解決 → uvicorn 起動。
# アプリ本体 (src/ymg_backend/main.py) は Phase 2 (T052) で実体化される。
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# ffmpeg は動画合成 (T081) で必須、 chromaprint は AcoustID (T078) で必須
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# 依存だけ先に解決してレイヤキャッシュを効かせる
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev --no-install-project

# アプリ本体
COPY . .
RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "ymg_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
