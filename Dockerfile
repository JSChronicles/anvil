# syntax=docker/dockerfile:1.7

FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS python-base

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv


FROM python-base AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /opt/anvil

# Install dependencies separately so source changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --locked \
        --no-dev \
        --extra all \
        --no-install-project

# Install Anvil itself after copying the source.
COPY README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --locked \
        --no-dev \
        --extra all \
        --no-editable


FROM python-base AS runtime

ARG VERSION=unknown
ARG REVISION=unknown
ARG CREATED=unknown

LABEL org.opencontainers.image.title="Anvil" \
      org.opencontainers.image.description="Provider-aware cloud task runner" \
      org.opencontainers.image.source="https://github.com/JSChronicles/anvil" \
      org.opencontainers.image.url="https://github.com/JSChronicles/anvil" \
      org.opencontainers.image.documentation="https://opsfoundry.dev/anvil/" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

RUN groupadd --gid 10001 anvil \
    && useradd \
        --uid 10001 \
        --gid anvil \
        --create-home \
        --shell /usr/sbin/nologin \
        anvil \
    && mkdir --parents /workspace \
    && chown anvil:anvil /workspace

COPY --from=builder /opt/anvil/.venv /opt/anvil/.venv

ENV HOME=/home/anvil \
    PATH=/opt/anvil/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER anvil
WORKDIR /workspace

ENTRYPOINT ["anvil"]