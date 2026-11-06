FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/app/data/bot_database.db

RUN groupadd --gid 10001 potato \
    && useradd --uid 10001 --gid potato --create-home --shell /usr/sbin/nologin potato

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --requirement requirements.lock

COPY --chown=potato:potato . .
RUN mkdir -p /app/data \
    && chown -R potato:potato /app/data

USER potato

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "potato_bot.healthcheck"]

CMD ["python", "-m", "potato_bot.main"]
