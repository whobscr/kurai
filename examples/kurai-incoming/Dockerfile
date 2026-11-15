# Kurai with this plugin: Kurai's image of the version the plugin is for, plus the plugin in one small layer.
ARG KURAI_VERSION=0.2.8
FROM ghcr.io/astral-sh/uv:0.9.7 AS uv

FROM ghcr.io/whobscr/kurai:${KURAI_VERSION}
USER root
RUN --mount=type=bind,from=uv,source=/uv,target=/bin/uv --mount=type=bind,source=.,target=/src,rw \
    uv pip install --python /app/.venv/bin/python --no-deps --no-sources --no-cache /src
USER kurai
