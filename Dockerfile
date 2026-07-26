# syntax=docker/dockerfile:1.7
#
# Multi-stage build producing a small, non-root production image.
#
# Why the stages are split
# ------------------------
# Dependencies change far less often than application code. Installing them in
# their own layer, before the source is copied, means a code-only change reuses
# the cached dependency layer — rebuilds go from minutes to seconds, which is
# the difference between CI being a feedback loop and being a queue.
#
# The `dev` and `production` targets share that base, so the two environments
# resolve identical dependency versions from the same lockfile. "Works locally,
# fails in staging" is usually a dependency-resolution difference.

ARG PYTHON_VERSION=3.14

# ---------------------------------------------------------------------------
# Base — the interpreter and uv, shared by every stage
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS base

# PYTHONDONTWRITEBYTECODE: .pyc files are dead weight in an immutable image.
# PYTHONUNBUFFERED: without it, logs sit in a pipe buffer and a crashing
#   container takes its final — most important — log lines with it.
# UV_COMPILE_BYTECODE: precompile at build time so the first request does not
#   pay for it.
# UV_LINK_MODE=copy: the cache mount and the venv are different filesystems,
#   where uv's default hardlinking silently falls back and warns.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependencies — cached independently of application code
# ---------------------------------------------------------------------------
FROM base AS dependencies

# Only the dependency manifests, so this layer is invalidated only when they
# change. `--frozen` fails if the lockfile disagrees with pyproject.toml rather
# than silently resolving something new — a build must never quietly ship a
# different dependency tree than the one that was tested.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Development — includes dev dependencies and build tooling
# ---------------------------------------------------------------------------
FROM dependencies AS dev

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Source is bind-mounted by compose rather than copied, so edits are picked up
# by the reloader without a rebuild.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
# Production — application code, no build tools, non-root
# ---------------------------------------------------------------------------
FROM dependencies AS production

# A container that runs as root runs as root on the host kernel if anything
# escapes. Creating an unprivileged user costs nothing and removes that class
# of escalation entirely.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home app

COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

# Exercises the readiness probe: it verifies the process can reach its
# dependencies, not merely that it is running.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status == 200 else 1)"

# No --reload in production: the reloader doubles memory, breaks the lifespan's
# ownership of long-lived resources, and makes shutdown non-deterministic.
#
# Worker count is left to the orchestrator via UVICORN_WORKERS. Prefer scaling
# replicas over in-container workers — replicas can be scheduled across nodes
# and rolled independently, and each carries its own connection pool, which is
# what actually bounds database load.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
