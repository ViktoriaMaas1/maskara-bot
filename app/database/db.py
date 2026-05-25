"""
PostgreSQL подключение через SQLAlchemy 2.0 async.

Архитектура:
- Один engine на всё приложение (пул соединений)
- AsyncSession через factory — каждый запрос/задача получает свою сессию
- get_db_session — dependency для FastAPI endpoints

Используется:
- Stage 11: журнал сделок (Trade Journal)
- Stage 12: AI memory (паттерны выигрышных/убыточных сделок)
- везде где нужна транзакционная БД-работа

Принцип: используем asyncpg (async драйвер) — FastAPI же async.
psycopg2 (sync) используется только для миграций Alembic.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------
# Базовый класс для всех моделей
# --------------------------------------------------------------
class Base(DeclarativeBase):
    """
    Все ORM-модели наследуются от Base.
    Alembic использует Base.metadata для автогенерации миграций.
    """
    pass


# --------------------------------------------------------------
# Глобальные engine + sessionmaker
# --------------------------------------------------------------
_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _build_async_dsn() -> str:
    """
    Преобразует sync DSN из config в async.
    Config даёт postgresql+psycopg2://...  (для Alembic)
    Здесь нужен postgresql+asyncpg://...   (для приложения)
    """
    settings = get_settings()
    sync_dsn = settings.postgres_dsn
    return sync_dsn.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


async def init_db() -> AsyncEngine:
    """
    Создаёт engine + sessionmaker, проверяет коннект.
    Вызывается из lifespan() в main.py.
    """
    global _engine, _sessionmaker
    settings = get_settings()

    _engine = create_async_engine(
        _build_async_dsn(),
        # Логируем SQL только в debug режиме
        echo=(settings.log_level.value == "DEBUG"),
        # Пул: 5 постоянных + до 10 overflow при нагрузке
        pool_size=5,
        max_overflow=10,
        # Проверять соединение перед использованием (защита от мёртвых)
        pool_pre_ping=True,
        # Переиспользовать соединения максимум 1 час
        pool_recycle=3600,
    )

    _sessionmaker = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        autoflush=False,
    )

    # Sanity check: попытаемся выполнить простой запрос
    from sqlalchemy import text
    async with _engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        result.scalar()

    logger.info(
        "PostgreSQL подключён",
        extra={"host": settings.postgres_host, "db": settings.postgres_db},
    )
    # Создать все таблицы из Base.metadata, если их нет.
    # Идемпотентно: существующие не трогает.
    from app.database import models  # noqa: F401 — регистрация моделей в Base.metadata
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Таблицы Base.metadata синхронизированы")

    return _engine


async def close_db() -> None:
    """Закрывает пул соединений. Вызывается в lifespan() при остановке."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
        logger.info("PostgreSQL соединение закрыто")


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Возвращает sessionmaker. Бросает если БД не инициализирована."""
    if _sessionmaker is None:
        raise RuntimeError(
            "БД не инициализирована. lifespan() не отработал — проверь main.py"
        )
    return _sessionmaker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — отдаёт сессию на время запроса.

    Использование:
        from fastapi import Depends
        from app.database.db import get_db_session
        from sqlalchemy.ext.asyncio import AsyncSession

        async def my_endpoint(db: AsyncSession = Depends(get_db_session)):
            await db.execute(...)

    Автоматически:
    - Открывает сессию
    - Делает rollback при исключении
    - Закрывает сессию в конце (даже если ошибка)
    """
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        # commit делается явно в коде — не автомагически
        # (важно: транзакция должна быть осознанной)


async def healthcheck_db() -> bool:
    """Проверка живости БД для /health endpoint."""
    if _engine is None:
        return False
    try:
        from sqlalchemy import text
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.warning("DB healthcheck failed: %s", e)
        return False
