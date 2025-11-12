FROM python:3.12-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy dependency file first for better layer caching
COPY pyproject.toml ./

# Install dependencies only
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --all-extras --compile-bytecode

# Copy only files needed for project installation (excluding scrapling_pick.py)
COPY --exclude=scrapling_pick.py . .

ENV UV_LINK_MODE=copy
# Install browsers and project in one optimized layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    uv sync --all-extras --compile-bytecode && \
    # Sync the full project to get all deps including playwright and camoufox
    # Update apt and install browser dependencies
    apt-get update && \
    uv run playwright install-deps chromium firefox && \
    uv run playwright install chromium && \
    uv run camoufox fetch --browserforge && \
    # Cleanup
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Create screenshots directory
RUN mkdir -p /app/screenshots
RUN mkdir -p /app/picks_data

# Copy the main script last so changes don't invalidate expensive layers
COPY scrapling_pick.py .

# Set entrypoint to run scrapling_pick.py
ENTRYPOINT ["uv", "run", "python", "scrapling_pick.py"]