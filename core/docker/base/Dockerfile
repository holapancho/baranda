# syntax=docker/dockerfile:1
#
# baranda-base: Python, git, the baranda package (guard + MCP + OpenTelemetry) with locked
# dependencies, and an unprivileged user. Connector images build FROM it.
#
# Build from the repository root:
#   docker build -f core/docker/base/Dockerfile -t baranda-base .
#
# Behind a TLS-intercepting proxy, pass its CA bundle as a build secret (used during the build only,
# never stored in the image), plus the usual proxy build args:
#   docker build --secret id=extra_ca,src=/path/to/ca.crt --build-arg https_proxy=... ...

ARG PYTHON_IMAGE=python:3.13-slim

# --- build: a wheel for baranda and its locked requirements -----------------------------------
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
    uv export --frozen --no-dev --package baranda --extra mcp --extra otel --no-emit-workspace \
        -o /out/requirements.txt \
 && uv build --wheel --out-dir /out core

# --- runtime -----------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# apt over HTTPS; git is needed by guard's git rules.
RUN --mount=type=secret,id=extra_ca \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && if [ -f /run/secrets/extra_ca ]; then CA="-o Acquire::https::CaInfo=/run/secrets/extra_ca"; else CA=""; fi \
 && apt-get $CA update \
 && apt-get $CA install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/ /tmp/build/
RUN --mount=type=secret,id=extra_ca \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir --require-hashes -r /tmp/build/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/build/baranda-*.whl \
 && rm -rf /tmp/build

RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin baranda \
 && mkdir -p /var/lib/baranda \
 && chown 10001:10001 /var/lib/baranda

# /var/lib/baranda is the only writable place the image expects (the audit log); mount a volume there.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BARANDA_WORKSPACE=/workspace
WORKDIR /tmp
USER 10001
