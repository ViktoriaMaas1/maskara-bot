# ============================================================
# MASKARA AI Trading Bot — Makefile
# ============================================================
# Все длинные команды собраны здесь.
# Запуск:  make <команда>      (например: make up, make test, make logs)
# Список:  make help
# ============================================================

.PHONY: help install up down restart logs ps test test-unit lint format \
        migrate migration shell db-shell redis-cli health clean

# Команда по умолчанию — показать список команд
.DEFAULT_GOAL := help

help:  ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'


# ============================================================
# Установка
# ============================================================

install:  ## Установить dev зависимости локально (для тестов и линтера)
	pip install -r requirements-dev.txt


# ============================================================
# Docker Compose
# ============================================================

up:  ## Запустить все сервисы (api + postgres + redis)
	docker compose up -d --build
	@echo "✅ Сервисы запущены. Проверь:  make health"

down:  ## Остановить все сервисы
	docker compose down

restart:  ## Перезапустить api (после изменений в коде)
	docker compose restart api

logs:  ## Показать логи api в реальном времени (Ctrl+C для выхода)
	docker compose logs -f api

logs-all:  ## Логи всех сервисов
	docker compose logs -f

ps:  ## Статус всех контейнеров
	docker compose ps


# ============================================================
# Healthcheck
# ============================================================

health:  ## Проверить что api жив (с учётом БД и Redis)
	@curl -fsS http://localhost:8000/health/ready | python3 -m json.tool || \
		echo "❌ Не отвечает или /ready вернул 503"


# ============================================================
# БД миграции (Alembic)
# ============================================================

migrate:  ## Применить миграции (alembic upgrade head)
	docker compose exec api alembic upgrade head

migration:  ## Создать миграцию из изменений в models.py.  Использование: make migration MSG="описание"
	@if [ -z "$(MSG)" ]; then echo "Укажи описание: make migration MSG=\"добавлено X\""; exit 1; fi
	docker compose exec api alembic revision --autogenerate -m "$(MSG)"

migrate-down:  ## Откатить последнюю миграцию
	docker compose exec api alembic downgrade -1

migrate-status:  ## Текущая версия БД
	docker compose exec api alembic current


# ============================================================
# Тесты и линтеры
# ============================================================

test:  ## Запустить все тесты
	docker compose exec api pytest

test-unit:  ## Только юнит-тесты (быстрые, без Postgres/Redis)
	docker compose exec api pytest -m unit

test-cov:  ## Тесты с coverage отчётом
	docker compose exec api pytest --cov=app --cov-report=term-missing

test-local:  ## Запустить тесты локально без Docker (нужен `pip install -r requirements-dev.txt`)
	pytest -m unit

lint:  ## Проверить код линтером
	ruff check app tests main.py

format:  ## Отформатировать код
	ruff format app tests main.py


# ============================================================
# Shell доступ
# ============================================================

shell:  ## Войти в контейнер api (bash)
	docker compose exec api bash

db-shell:  ## Открыть psql для нашей БД
	docker compose exec postgres psql -U $$(grep POSTGRES_USER .env | cut -d= -f2) \
		-d $$(grep POSTGRES_DB .env | cut -d= -f2)

redis-cli:  ## Открыть redis-cli
	docker compose exec redis sh -c 'redis-cli -a $$REDIS_PASSWORD' \
		2>/dev/null || docker compose exec -e REDIS_PASSWORD=$$(grep REDIS_PASSWORD .env | cut -d= -f2) redis redis-cli -a $$REDIS_PASSWORD


# ============================================================
# Утилиты
# ============================================================

clean:  ## Удалить всё: контейнеры + volumes (ОСТОРОЖНО — потеряешь данные БД!)
	@echo "⚠️  Это удалит ВСЕ данные БД. Точно? (Ctrl+C для отмены)"
	@sleep 3
	docker compose down -v
	@echo "✅ Всё очищено"

env-check:  ## Проверить что .env существует и заполнен
	@if [ ! -f .env ]; then \
		echo "❌ .env не найден. Скопируй: cp .env.example .env"; exit 1; \
	fi
	@if grep -q "CHANGE_ME" .env; then \
		echo "❌ В .env остались CHANGE_ME — заполни секреты"; exit 1; \
	fi
	@echo "✅ .env выглядит готовым"
