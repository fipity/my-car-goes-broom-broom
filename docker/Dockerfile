# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --create-home --uid 1000 carscout
COPY pyproject.toml ./
COPY carscout ./carscout
RUN pip install ".[dev]"
COPY alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY tests ./tests
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R carscout /app
USER carscout

# --- API + UI: runs migrations, then serves ---
FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]

# --- Worker: scheduler + scrapers. Playwright/Chromium is added here in phase 4. ---
FROM base AS worker
ENTRYPOINT ["/entrypoint.sh"]
CMD ["worker"]
