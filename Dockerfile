FROM python:3.13-slim

# uv manages the locked dependencies, the same way it does on your PC
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first, so this layer is reused when only sage.py changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY sage.py ./

# Download the Silero and turn-detector model files at build time,
# so the container doesn't fetch them on every start
RUN python sage.py download-files

# Production mode: connects to the LiveKit server and waits for dispatched jobs.
# Settings come from environment variables set in Coolify, not from a .env file.
CMD ["python", "sage.py", "start"]
