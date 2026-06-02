# MASKARA-BOT: Состояние проекта (обновлено 2 июня 2026, 14:00 UTC)

## Инфраструктура
- Сервер: 204.168.140.135, `/root/maskara-bot`. Контейнеры: maskara_api, maskara_postgres, maskara_redis (все healthy).
- `app/` монтируется как `/app/app:ro`. Корневой `main.py` → `/app/main.py`. Точка входа: `uvicorn main:app`.
- **Перезапуск:** правки кода → `docker compose restart api`. Правки `.env` → `docker compose up -d api`.
- Git: только на ПК (`C:\Users\vikto\Downloads\maskara-bot-stage1\maskara-bot`). GitHub: `github.com/ViktoriaMaas1/maskara-bot`, ветка `main`.
- Redis: `max_connections=500` (НЕ менять).

## ЧТО СДЕЛАНО СЕГОДНЯ (2 июня 2026)

### Stage 8 — Signal Generator (продолжение)
- Dashboard Basic Auth дорабо­тан и протестирован: авт нормально блокирует неавторизованные запросы.
- Redis `max_connections`: эксперимент 500→50 провалился, откат на 500 (рабочее значение).

### Stage 9 — Liquidity Engine, Фаза 1 ✅ ЗАКРЫТА
**Полностью реализовано и протестировано:**

| Компонент | Строк | Статус |
|-----------|-------|--------|
| `models.py` | 82 | ✅ ZoneSide enum, LiquidityZone, LiquiditySnapshot |
| `detector.py` | 144 | ✅ find_orderbook_walls, detect_local_highs_lows, count_recent_liquidations |
| `conftest.py` | 71 | ✅ Фикстуры с реальными форматами данных |
| `test_detector.py` | 104 | ✅ 9 тестов, все PASSED |
| **ИТОГО** | **401** | **✅ Протестировано, работает** |

**Что работает:**
- Чтение orderbook в реальном формате `{"b": [...], "a": [...]}`
- Детектирование крупных "стенок" в стакане (выше/ниже цены)
- Поиск локальных максимумов/минимумов в свежих сделках
- Подсчёт ликвидаций с фильтрацией мусора (нулевые цены, пустой side)
- Все функции — чистые (без побочных эффектов), легко тестировать

**Фаза 1 стоп-критерий:** 9/9 тестов зелёные ✅

## ПОЛНАЯ КАРТА ПРОЕКТА (работающее ПО)

| Стадия | Что | Коммит | Статус |
|--------|-----|--------|--------|
| Stage 1 | FastAPI webhook server | в git | ✅ |
| Stage 2 | Bybit REST client (async, READONLY) | в git | ✅ |
| Stage 3 | Risk Manager (11 проверок) | в git | ✅ |
| Stage 4 | Execution Engine + Telegram | в git | ✅ |
| Stage 5 | Bybit WebSocket (5 потоков, auto-reconnect) | в git | ✅ |
| Stage 6 | Real-Time Market Cache (Redis) | в git | ✅ |
| Stage 7 | Order Flow Engine (5 метрик, 35 тестов) | `f60f75f` | ✅ |
| Stage 8 | Signal Generator (67 тестов) + Dashboard Auth | `6073c57` | ✅ |
| Stage 9 | **Liquidity Engine — Фаза 1** | TBD | ✅ Фаза 1 |
| Доп. | Dashboard HTML + 5 API-эндпоинтов | `6073c57` | ✅ |

## В РАБОТЕ — Liquidity Engine, Фазы 2–4

**Фаза 2** (engine.py + чтение market_cache):
- `LiquidityEngine` класс
- Чтение orderbook/trades/liquidations из market_cache
- Сборка `LiquiditySnapshot` — снимок состояния

**Фаза 3** (API endpoint):
- `app/api/liquidity.py` → `/liquidity/{symbol}`
- Аналогия с order_flow

**Фаза 4** (опционально):
- Интеграция в дашборд

## ИЗВЕСТНЫЕ ТЕХДОЛГИ
1. **Пороги сигналов не читаются из .env** — захардкодены в `rules.py`.
2. **CVD теряется при рестарте** — in-memory, нужно в Redis.
3. **.bak-файлы на сервере** — добавить в .gitignore.
4. **.bak-файлы** уже есть в `.gitignore` (вроде, надо проверить).

## КЛЮЧЕВЫЕ ФАЙЛЫ
- `/root/maskara-bot/app/engines/liquidity/models.py` — данные
- `/root/maskara-bot/app/engines/liquidity/detector.py` — логика анализа
- `/root/maskara-bot/tests/liquidity/test_detector.py` — тесты (9/9 PASSED)
- `/root/maskara-bot/main.py` — точка входа (строка 25, 148: auth для dashboard)
- `/root/maskara-bot/app/config.py` — конфигурация

## NEWS & SOCIAL ENGINE — Status
- `NEWS_API_KEY` в .env: **пустой** (0 символов)
- Рекомендация: получить ключ CryptoCompare или NewsData.io, потом стартовать Stage 10
- Сейчас: Liquidity Engine приоритет (данные уже есть, ключи не нужны)

## КАК РАБОТАТЬ С ВИКТОРОМ
- SSH (сервер) и PowerShell (ПК) — явно указывать куда команда
- Команды готовыми блоками для копирования
- Скриншоты вывода перед действиями, не гадать
- Никогда не менять прод вслепую

## NEXT SESSION CHECKLIST
- [ ] git add `app/engines/liquidity/` `tests/liquidity/` `PROJECT_STATE.md`
- [ ] git commit "Add Liquidity Engine Phase 1: models + detector + 9 tests (401 lines, all PASSED)"
- [ ] git push
- [ ] Если нужен News: получить API ключ (CryptoCompare или NewsData.io)
- [ ] Фаза 2 Liquidity Engine: engine.py + market_cache чтение
