FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PYTHON=3.11
ENV HOST=0.0.0.0
ENV PIPECAT_TRANSPORT=daily

COPY pyproject.toml uv.lock bot.py ./

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-7860}/health" || exit 1'

CMD ["python", "bot.py"]
