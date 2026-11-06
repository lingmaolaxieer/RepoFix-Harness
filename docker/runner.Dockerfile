FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep patch \
    && pip install --no-cache-dir pytest pytest-asyncio ruff \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 runner

USER runner
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

