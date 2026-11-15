# Kurai's image: the web app and the worker (the same image, `kurai web` / `kurai worker`).
# Python goes in two layers: the libraries (rebuilt only when uv.lock's libraries change) and Kurai itself (a few MB),
# so a release downloads only Kurai's own layer.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.7

# pyproject.toml and uv.lock without Kurai's version: the library layer's cache key
FROM python:3.12-slim AS lock
WORKDIR /lock
COPY pyproject.toml uv.lock docker/without-version.py ./
RUN python without-version.py && rm without-version.py && touch -d @0 pyproject.toml uv.lock

# Kurai as a wheel
FROM ${UV_IMAGE} AS uv
FROM python:3.12-slim AS wheel
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=bind,from=uv,source=/uv,target=/bin/uv uv build --wheel --out-dir /dist

FROM python:3.12-slim
RUN useradd --uid 1000 --create-home kurai
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
# the libraries
# copied, not mounted: a copy's cache key is the files' contents, so a new version number doesn't rebuild the libraries
COPY --from=lock /lock /lock
RUN --mount=type=bind,from=uv,source=/uv,target=/bin/uv cd /lock && uv sync --frozen --no-dev --no-install-project --no-cache
# Kurai itself
RUN --mount=type=bind,from=uv,source=/uv,target=/bin/uv --mount=type=bind,from=wheel,source=/dist,target=/dist \
    uv pip install --python /app/.venv/bin/python --no-deps --no-cache /dist/*.whl
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER kurai
WORKDIR /app
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"KURAI_WEB_PORT\",\"8095\")}/healthz')" || exit 1
CMD ["kurai", "web"]
