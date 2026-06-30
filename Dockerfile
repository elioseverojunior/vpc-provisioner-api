# ==========================================
# Stage 1: Base Installation
# ==========================================
FROM python:3.12-slim AS base

USER root

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# ==========================================
# Stage 2: Build & Dependency Installation
# ==========================================
FROM base AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory and environment variables
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy dependency files first to leverage Docker caching
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Copy the rest of the application source code
COPY . .

# Sync the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ==========================================
# Stage 3: Minimal Runtime Image (Jailed / Hardened)
# ==========================================
FROM base AS runtime

# 1. Create a non-root system user and group with explicit IDs
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -m -s /sbin/nologin appuser

WORKDIR /app

# 2. Copy dependencies and source code, transferring ownership to root,
#    but allowing appuser read-only access (or read/write if explicitly required).
#    For maximum security, keep files owned by root and readable by appuser.
COPY --from=builder --chown=root:appgroup /app/.venv /app/.venv
COPY --chown=root:appgroup ./serverless/ /app/

# Set environment variables to use the virtual environment automatically
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AWS_REGION="us-east-1"

# 3. Switch to the unprivileged user
USER 10001

# Expose your application port (Note: AWS Lambda ignores EXPOSE, but good for local dev)
EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Define how to run your application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]