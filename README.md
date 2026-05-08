# MASKARA AI Trading Bot

Профессиональный AI trading bot для Bybit Futures в стиле hedge-fund trading system.

## Главная идея

Бот не торгует по одному индикатору. Он собирает данные с рынка, стакана,
ликвидности, новостей, социальных сетей, TradingView, анализирует всё через AI,
считает вероятность сделки, проверяет риск и только потом открывает позицию.

## Стек

- Python 3.11+
- FastAPI (webhook сервер)
- pybit (Bybit Unified V5 API)
- PostgreSQL (журнал сделок, AI память)
- Redis (кэш, rate limit, дедупликация)
- Docker + Docker Compose
- SQLAlchemy 2 + Alembic
- Pydantic v2
- Telegram Bot API
- pytest

## Безопасность

- Только testnet до прохождения 100+ тестовых сделок и 30 дней forward test
- Mainnet запрещён до явного одобрения человеком
- Все секреты только в `.env`
- Webhook защищён secret token + rate limit + дедупликация

## Этапы разработки

См. полный список stages в `docs/stages.md` (создаётся по мере разработки).

Сейчас: **Stage 1 — FastAPI Webhook Server**

## Структура

```
app/
  api/          # FastAPI endpoints (webhook, healthcheck)
  bybit/        # REST + WebSocket клиенты Bybit
  engines/      # Liquidity, OrderFlow, AI Decision, Risk и т.д.
  database/     # SQLAlchemy модели, репозитории
  telegram/     # Telegram уведомления и команды
  dashboard/    # Web dashboard (позже)
  utils/        # Вспомогательные модули (логи, безопасность)
tests/          # pytest
logs/           # файлы логов
scripts/        # утилиты (миграции, тесты webhook)
```

## Запуск (после Stage 1)

```bash
cp .env.example .env       # заполнить секреты
docker-compose up --build
```
