FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH=/root/.fly/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin

# git + flyctl let the agent check out, edit, push and redeploy its own
# source (self-improvement). sqlite3 is for cross-chat history queries.
# nodejs/npm are for the Codex CLI (OpenAI backend).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates sqlite3 git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for Python dependency management. The claude-agent-sdk Python
# package bundles its own claude CLI binary, so no separate install needed.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# flyctl so the agent can deploy itself.
RUN curl -L https://fly.io/install.sh | sh

# Codex CLI for the OpenAI backend (AGENT_BACKEND=codex).
RUN npm install -g @openai/codex

# Install Python dependencies first so they cache independently of source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/
COPY prompts/ ./prompts/

CMD ["uv", "run", "--no-dev", "python", "src/bot.py"]
