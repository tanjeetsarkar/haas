FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Non-root execution user
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 haas && useradd -u 10001 -g haas -m -s /bin/bash haas

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY plugins ./plugins

RUN pip install --upgrade pip && pip install . \
    && mkdir -p /tmp/haas_scratchpad \
    && chown -R haas:haas /app /tmp/haas_scratchpad

USER haas

EXPOSE 8000
