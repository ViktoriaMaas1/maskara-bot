# ============================================================
# MASKARA AI Trading Bot — FastAPI Dockerfile
# ============================================================
# Multi-stage build для маленького и безопасного образа.
#
# Сборка production образа (по умолчанию):
#   docker build -t maskara:prod .
#
# Сборка dev образа (с тестами и линтерами):
#   docker build -t maskara:dev --build-arg INSTALL_DEV=true .
#
# В docker-compose.yml сейчас собирается prod — для тестов
# в Stage 1 можно переключить:  build: { args: { INSTALL_DEV: "true" } }
# ============================================================

ARG INSTALL_DEV=true     # для удобства разработки. В проде — false.

# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

ARG INSTALL_DEV

WORKDIR /build

# Системные зависимости для сборки (psycopg2, asyncpg, и т.д.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Сначала только requirements — кешируется отдельно от кода
COPY requirements.txt requirements-dev.txt ./

# Ставим в отдельную директорию для копирования в финальный образ
RUN pip install --no-cache-dir --upgrade pip && \
    if [ "$INSTALL_DEV" = "true" ]; then \
        pip install --no-cache-dir --prefix=/install -r requirements-dev.txt; \
    else \
        pip install --no-cache-dir --prefix=/install -r requirements.txt; \
    fi


# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

ARG INSTALL_DEV

# Минимум runtime зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Непривилегированный пользователь — security best practice
RUN groupadd -r maskara && useradd -r -g maskara -d /app -s /bin/bash maskara

WORKDIR /app

# Копируем только установленные пакеты из builder
COPY --from=builder /install /usr/local

# Копируем код приложения
COPY --chown=maskara:maskara app /app/app
COPY --chown=maskara:maskara main.py /app/main.py
COPY --chown=maskara:maskara alembic /app/alembic
COPY --chown=maskara:maskara alembic.ini /app/alembic.ini

# В dev образ кладём тесты и pytest конфиг (для `make test`)
# В .dockerignore tests/ исключен — переопределяем через условный COPY
COPY --chown=maskara:maskara tests /app/tests
COPY --chown=maskara:maskara pytest.ini /app/pytest.ini

# Папка для логов
RUN mkdir -p /app/logs && chown -R maskara:maskara /app/logs

USER maskara

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production: 1 воркер. Если нужно больше — настраиваем в docker-compose командой.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
