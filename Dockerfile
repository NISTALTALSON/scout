FROM python:3.12.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/scout/.venv \
    SCOUT_STATE_DIR=/opt/scout/data

WORKDIR /opt/scout
RUN python -m pip install --no-cache-dir uv==0.11.28

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY scout ./scout
COPY config ./config
COPY docs ./docs
COPY README.md .env.example ./
RUN uv sync --frozen --no-dev && useradd --create-home --uid 10001 scout \
    && mkdir -p /opt/scout/data && chown -R scout:scout /opt/scout

USER scout
VOLUME ["/opt/scout/data"]
CMD ["/opt/scout/.venv/bin/python", "-m", "scout.cli", "scan", "--dry-run", "--no-email"]
