# Инструкция запуска Stage 1

Пошаговая проверка что всё работает. Тестируй сначала локально или на тестовом сервере — **не на проде**.

---

## Требования

- Docker 24+ и Docker Compose v2
- 2 ГБ свободной памяти, 5 ГБ диска
- Открытый порт 8000 (или другой через `API_PORT` в `.env`)

Проверка что Docker есть:
```bash
docker --version
docker compose version
```

---

## Шаг 1. Подготовка окружения

### 1.1. Клонировать или загрузить проект

Если на твой Hetzner сервер:
```bash
mkdir -p /root/maskara-bot
cd /root/maskara-bot
# распаковать или скопировать сюда все файлы проекта
```

### 1.2. Создать `.env` из шаблона

```bash
cp .env.example .env
```

### 1.3. Сгенерировать секреты и заполнить `.env`

```bash
# Генерируем 3 случайных секрета для .env:
echo "WEBHOOK_SECRET=$(openssl rand -hex 32)"
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')"
echo "REDIS_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')"
```

Открой `.env` в редакторе и подставь значения. **Проверь** что нигде не осталось `CHANGE_ME` — приложение откажется стартовать.

```bash
nano .env       # или vi .env
```

### 1.4. Проверка `.env`

```bash
make env-check
```

Должно показать `✅ .env выглядит готовым`.

---

## Шаг 2. Запуск сервисов

```bash
make up
```

Эта команда:
- Собирает Docker образ (первый раз ~3-5 минут)
- Запускает 3 контейнера: `maskara_api`, `maskara_postgres`, `maskara_redis`
- Сеть, volumes и healthchecks настраиваются автоматически

Проверка что контейнеры живы:
```bash
make ps
```

Должно показать **3 сервиса в state Up**, postgres и redis должны быть `healthy`.

---

## Шаг 3. Применение миграций БД

```bash
# Сначала генерируем миграцию из моделей
docker compose exec api alembic revision --autogenerate -m "initial schema"

# Применяем
make migrate
```

Проверка:
```bash
make migrate-status
```

Должно показать ID последней применённой миграции.

Можно зайти в БД и убедиться что таблицы созданы:
```bash
make db-shell
# В psql:
\dt          # должно показать 7 таблиц + alembic_version
\q
```

---

## Шаг 4. Проверка healthcheck

```bash
make health
```

Должен вернуть:
```json
{
  "status": "ready",
  "checks": {
    "postgres": true,
    "redis": true
  }
}
```

Если что-то `false` — смотри логи: `make logs`.

---

## Шаг 5. Запуск тестов

```bash
make test
```

Должно пройти **44 теста**. Если хоть один упал — стоп, разбираемся.

---

## Шаг 6. Тест webhook

### 6.1. Корректный сигнал
```bash
python3 scripts/test_webhook.py
```

Ожидаемый ответ: `HTTP 202 → status=accepted`

### 6.2. Дедупликация
```bash
python3 scripts/test_webhook.py --count 3
```

Первый: `accepted`. Второй и третий: `duplicate`.

### 6.3. Rate limit
```bash
# В .env по умолчанию WEBHOOK_RATE_LIMIT_PER_MIN=30 — отправляем 35
python3 scripts/test_webhook.py --count 35 --delay 0.05
```

Сначала пройдут (либо `accepted`, либо `duplicate`). После 30-го должны пойти `HTTP 429`.

### 6.4. Невалидный secret
```bash
python3 scripts/test_webhook.py --bad-secret
```

Ожидание: `HTTP 401`.

### 6.5. Запрещённый символ
```bash
python3 scripts/test_webhook.py --symbol DOGEUSDT
```

DOGE не в whitelist `ALLOWED_SYMBOLS` → `HTTP 403`.

---

## Шаг 7. Проверка логов

В реальном времени:
```bash
make logs
```

Файлы логов на хосте — папка `logs/`:
```bash
ls -la logs/
tail -f logs/maskara.log     # JSON формат
```

Поиск по `request_id` (если знаешь его):
```bash
cat logs/maskara.log | jq 'select(.request_id == "abc-123-xxx")'
```

Поиск всех webhook от конкретного IP:
```bash
cat logs/maskara.log | jq 'select(.ip == "1.2.3.4")'
```

---

## Шаг 8. TradingView (опционально, после Stage 4)

⚠️ Сейчас бот **не торгует** — только принимает и логирует сигналы. Подключение к TradingView имеет смысл начиная со Stage 4. Но проверить связь можно уже сейчас.

Webhook URL для TradingView:
```
http://YOUR_SERVER_IP:8000/webhook
```

Сообщение в alert:
```json
{
  "secret": "ТВОЙ_WEBHOOK_SECRET_ИЗ_ENV",
  "symbol": "{{ticker}}",
  "side": "BUY",
  "timeframe": "{{interval}}",
  "strategy": "ema_cross"
}
```

После срабатывания alert смотри:
```bash
make logs   # должен появиться "Webhook принят"
```

---

## Если что-то сломалось

### Сервисы не стартуют
```bash
make logs-all      # логи всех контейнеров
make ps            # какие в Up, какие в Exit
```

### Контейнер api перезапускается
Скорее всего ошибка валидации `.env`. Запусти один раз и смотри:
```bash
docker compose run --rm api python -c "from app.config import get_settings; get_settings()"
```

Pydantic покажет точно что не так с `.env`.

### Тесты падают
```bash
docker compose exec api pytest -vv --tb=long
```

### "Port 8000 already in use"
Поменяй `API_PORT` в `.env` на другой (например 8080) и `make restart`.

### Полный сброс (потеря данных БД!)
```bash
make clean
make up
make migrate
```

---

## Что дальше

Если всё прошло:
- ✅ контейнеры живы
- ✅ `/health/ready` возвращает 200
- ✅ 44 теста зелёные
- ✅ webhook принимает сигнал, дедуп и rate limit работают

→ **Stage 1 ЗАВЕРШЁН.** Можно переходить к **Stage 2: подключение Bybit testnet REST API**.
