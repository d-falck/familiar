FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for Python dependency management.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the Claude Code CLI (required at runtime by claude-agent-sdk).
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install Python dependencies first so they cache independently of source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/

CMD ["uv", "run", "--no-dev", "python", "src/bot.py"]
