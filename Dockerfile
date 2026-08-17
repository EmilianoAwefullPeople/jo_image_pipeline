FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    JO_WEB_HOST=0.0.0.0 \
    JO_WEB_PORT=8080 \
    JO_WEB_RUNS_DIR=/app/data/web_runs \
    JO_LOG_LEVEL=INFO

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY jo_pipeline ./jo_pipeline
COPY llm_pipeline ./llm_pipeline
COPY jo_web ./jo_web

RUN mkdir -p /app/data/web_runs

EXPOSE 8080

CMD ["python", "-m", "jo_web"]
