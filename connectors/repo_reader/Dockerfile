# syntax=docker/dockerfile:1
#
# repo_reader connector: baranda-base + read-only CLIs + the connector package.
#
# Build from the repository root, after baranda-base:
#   docker build -f connectors/repo_reader/Dockerfile -t baranda-repo-reader .
# (docker compose builds both in order.)
#
# Run over stdio with the repository mounted read-only:
#   docker run -i --rm -v /path/to/repo:/workspace:ro baranda-repo-reader

ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
RUN --mount=type=secret,id=extra_ca \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir uv==0.11.32
COPY pyproject.toml uv.lock ./
COPY core/ core/
COPY connectors/ connectors/
RUN --mount=type=secret,id=extra_ca \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv build --wheel --out-dir /out connectors/repo_reader

# "baranda-base" is the image built from core/docker/base/Dockerfile (or the compose service).
FROM baranda-base

USER root
RUN --mount=type=secret,id=extra_ca \
    if [ -f /run/secrets/extra_ca ]; then CA="-o Acquire::https::CaInfo=/run/secrets/extra_ca"; else CA=""; fi \
 && apt-get $CA update \
 && apt-get $CA install -y --no-install-recommends ripgrep jq tree fd-find \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/ /tmp/build/
RUN pip install --no-cache-dir --no-deps /tmp/build/*.whl && rm -rf /tmp/build

USER 10001
ENTRYPOINT ["baranda-repo-reader"]
