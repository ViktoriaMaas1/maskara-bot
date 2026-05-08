"""
Alembic environment.

Особенности нашей конфигурации:
- DSN читаем из app.config (sync DSN — Alembic работает синхронно)
- target_metadata = Base.metadata — для autogenerate миграций
- ВАЖНО: импортируем все модели чтобы Alembic их увидел
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# --------------------------------------------------------------
# Импортируем приложение
# --------------------------------------------------------------
from app.config import get_settings
from app.database.db import Base

# КРИТИЧНО: импортируем все модели чтобы они зарегистрировались в Base.metadata
import app.database.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Подсовываем sync DSN для Alembic (psycopg2)
config.set_main_option("sqlalchemy.url", get_settings().postgres_dsn)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Генерирует SQL без подключения к БД (для CI/CD)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Применяет миграции с подключением к БД."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
