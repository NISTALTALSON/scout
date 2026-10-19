# Python 3.12.13-slim multi-architecture manifest, resolved 2026-10-19.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/scout/.venv \
    SCOUT_STATE_DIR=/opt/scout/data

WORKDIR /opt/scout
RUN python -m pip install --no-cache-dir uv==0.11.28

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

COPY scout ./scout
COPY config ./config
COPY docs ./docs
COPY scripts ./scripts
COPY tests ./tests
COPY demo ./demo
COPY README.md LICENSE .env.example ./
RUN uv sync --frozen && useradd --create-home --uid 10001 scout \
    && mkdir -p /opt/scout/data && chown -R scout:scout /opt/scout

USER scout
VOLUME ["/opt/scout/data"]
CMD ["/opt/scout/.venv/bin/python", "-m", "scout.cli", "scan", "--dry-run", "--no-email"]
