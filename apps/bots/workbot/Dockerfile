FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOTWORK_DB=/data/botwork.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && useradd --uid 10001 --create-home botwork && mkdir /data && chown botwork:botwork /data
COPY botwork ./botwork
COPY config.toml .
USER 10001:10001
VOLUME ["/data"]
CMD ["python", "-m", "botwork", "run"]
