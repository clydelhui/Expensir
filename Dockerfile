FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY expensir ./expensir
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# Run `uv run alembic upgrade head` (and setWebhook) as a one-shot release step, not per cold start (§14)
CMD ["uv", "run", "--no-sync", "python", "-m", "expensir"]
