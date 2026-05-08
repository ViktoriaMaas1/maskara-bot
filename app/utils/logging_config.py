"""
Логирование — единая точка настройки.

Принципы:
- stdout: человекочитаемый формат для `docker logs` и разработки
- файл: JSON формат с ротацией для долгосрочного хранения и парсинга
- request_id: автоматически добавляется к каждой записи (если есть в extra)
- секреты: маскируются на уровне фильтра — даже если случайно залогируем

Использование везде в коде:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("сообщение", extra={"request_id": rid, "symbol": "BTCUSDT"})
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any


# --------------------------------------------------------------
# Что считаем секретом — никогда не пишем в логи
# --------------------------------------------------------------
_SECRET_KEYS: set[str] = {
    "secret",
    "password",
    "passwd",
    "api_key",
    "api_secret",
    "token",
    "authorization",
    "auth",
    "bybit_api_key",
    "bybit_api_secret",
    "telegram_bot_token",
    "coinglass_api_key",
    "webhook_secret",
}

# Паттерн для маскирования в текстах сообщений (на всякий случай)
# Ищет: api_key=xxxxx, "secret": "yyy", token=zzz и подобные
_SECRET_PATTERN = re.compile(
    r'((?:' + '|'.join(_SECRET_KEYS) + r')["\']?\s*[:=]\s*["\']?)([^\s"\',}]+)',
    re.IGNORECASE,
)


def _mask_value(value: Any) -> Any:
    """Заменяет потенциально секретное значение на ***"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return _mask_dict(value) if isinstance(value, dict) else [_mask_value(v) for v in value]
    s = str(value)
    if len(s) <= 4:
        return "***"
    # Показываем первые 2 и последние 2 символа — для диагностики, не выдавая значение
    return f"{s[:2]}***{s[-2:]}"


def _mask_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно маскирует секретные ключи в dict."""
    result = {}
    for k, v in data.items():
        if k.lower() in _SECRET_KEYS:
            result[k] = _mask_value(v)
        elif isinstance(v, dict):
            result[k] = _mask_dict(v)
        elif isinstance(v, list):
            result[k] = [_mask_dict(item) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


# --------------------------------------------------------------
# Фильтр: дополняет каждую запись и маскирует секреты
# --------------------------------------------------------------
class SecretFilter(logging.Filter):
    """
    Маскирует секреты в любом логе:
    - в самом сообщении (через regex)
    - в extra-полях (через рекурсивный обход)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Маскируем в основном сообщении
        if isinstance(record.msg, str):
            record.msg = _SECRET_PATTERN.sub(r'\1***', record.msg)

        # Маскируем в extra-полях (всё что не стандартные атрибуты LogRecord)
        for key in list(record.__dict__.keys()):
            if key in _SECRET_KEYS:
                record.__dict__[key] = _mask_value(record.__dict__[key])
            elif isinstance(record.__dict__[key], dict):
                record.__dict__[key] = _mask_dict(record.__dict__[key])

        return True


# --------------------------------------------------------------
# JSON форматтер — для записи в файл
# --------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """
    Форматирует запись как JSON. Удобно для:
    - grep по полям: `cat maskara.log | jq 'select(.symbol=="BTCUSDT")'`
    - импорта в Loki / ELK / Datadog
    """

    # Стандартные атрибуты LogRecord — не дублируем их в extra
    _STANDARD_ATTRS = {
        "name", "msg", "args", "created", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs",
        "message", "pathname", "process", "processName",
        "relativeCreated", "thread", "threadName", "exc_info",
        "exc_text", "stack_info", "taskName", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Добавляем extra-поля (request_id, symbol и т.д.)
        for key, value in record.__dict__.items():
            if key not in self._STANDARD_ATTRS and not key.startswith("_"):
                log_data[key] = value

        # Исключение — сериализуем traceback
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # default=str — на случай Decimal, datetime, UUID и др.
        return json.dumps(log_data, ensure_ascii=False, default=str)


# --------------------------------------------------------------
# Человекочитаемый форматтер — для stdout
# --------------------------------------------------------------
class HumanFormatter(logging.Formatter):
    """
    Простой формат для разработки:
        2026-05-08 15:32:01 [INFO] app.api.webhook: Webhook принят (request_id=abc123 symbol=BTCUSDT)
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in JsonFormatter._STANDARD_ATTRS and not k.startswith("_")
        }
        if extras:
            extra_str = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} ({extra_str})"
        return base


# --------------------------------------------------------------
# Главная функция настройки
# --------------------------------------------------------------
def setup_logging(
    level: str = "INFO",
    log_file: Path | str = "logs/maskara.log",
    max_bytes: int = 50 * 1024 * 1024,   # 50 MB на файл
    backup_count: int = 10,              # 10 ротированных копий = ~500 MB макс
) -> None:
    """
    Настраивает корневой логгер.

    Вызывается ОДИН раз — в main.py при старте приложения.

    Args:
        level: DEBUG / INFO / WARNING / ERROR
        log_file: путь к файлу логов
        max_bytes: максимальный размер файла перед ротацией
        backup_count: сколько ротированных файлов хранить
    """
    # Создаём папку для логов если нет
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Сбрасываем все хендлеры — на случай повторной настройки (тесты)
    root.handlers.clear()

    secret_filter = SecretFilter()

    # ---------- stdout: человекочитаемый ----------
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        HumanFormatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    stdout_handler.addFilter(secret_filter)
    root.addHandler(stdout_handler)

    # ---------- файл: JSON + ротация ----------
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(secret_filter)
    root.addHandler(file_handler)

    # Гасим шумные сторонние логгеры
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("urllib3").setLevel("WARNING")
    logging.getLogger("websockets").setLevel("WARNING")

    # Проверочная запись чтобы убедиться что всё работает
    logging.getLogger(__name__).info(
        "Logging инициализирован",
        extra={"level": level, "log_file": str(log_path)},
    )
