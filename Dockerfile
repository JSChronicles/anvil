# syntax=docker/dockerfile:1.7

FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS python-base

FROM ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f AS uv

FROM python-base AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /opt/anvil

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --locked --no-dev --extra all --no-editable

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
    && useradd --uid 10001 --gid anvil --create-home --shell /usr/sbin/nologin anvil \
    && mkdir --parents /opt/anvil /workspace \
    && chown anvil:anvil /workspace

COPY --from=builder /opt/anvil/.venv /opt/anvil/.venv

ENV HOME=/home/anvil \
    PATH=/opt/anvil/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER anvil
WORKDIR /workspace

ENTRYPOINT ["anvil"]
