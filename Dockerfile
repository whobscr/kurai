# Kurai's image: the web app and the worker (the same image, `kurai web` / `kurai worker`).
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --uid 1000 --create-home kurai
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER kurai
WORKDIR /app
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"KURAI_WEB_PORT\",\"8095\")}/healthz')" || exit 1
CMD ["kurai", "web"]
