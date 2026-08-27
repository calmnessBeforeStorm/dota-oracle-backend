FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install -e .

COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Dev image: same thing plus the test and lint toolchain. Database-backed tests need a real
# Postgres and a Python with asyncpg, which is exactly what this container has, so the suite
# is meant to be run inside it - `docker compose exec api python -m pytest`.
FROM base AS dev
RUN pip install -e ".[dev]"


# ML image: the training pipeline (phase 4). Kept out of `base` on purpose - the API process
# only ever loads a trained booster, and LightGBM plus scikit-learn have no business in the
# image that serves requests. Training is run as a script, never from the API
# (spec section 9.3).
FROM dev AS ml
RUN pip install "lightgbm>=4.5" "scikit-learn>=1.6" "numpy>=2.1"
