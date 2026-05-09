"""
Кастомные исключения для Bybit клиента.

Иерархия:
    BybitError                  — базовое (отлавливает всё)
        BybitAuthError          — невалидные/истёкшие ключи (НЕ ретраим)
        BybitNetworkError       — таймаут, сеть упала (можно ретраить)
        BybitRateLimitError     — слишком много запросов (ретрай с backoff)
        BybitAPIError           — Bybit вернул ошибку (анализируем code)

Зачем разделение: разные ошибки требуют разной реакции.
- Auth → стоп бот, отправить алерт админу
- Network → ретрай с экспоненциальной задержкой
- RateLimit → ретрай с большой задержкой
- API → разбор по коду, может быть и фатально, и нет
"""

from __future__ import annotations

from typing import Optional


class BybitError(Exception):
    """Базовое исключение Bybit клиента."""


class BybitAuthError(BybitError):
    """Невалидные API ключи, неверная подпись, истёкший recv_window."""


class BybitNetworkError(BybitError):
    """Сетевая ошибка: таймаут, DNS, обрыв соединения."""


class BybitRateLimitError(BybitError):
    """Превышен лимит запросов."""


class BybitAPIError(BybitError):
    """Bybit вернул ошибку с retCode != 0."""

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        method: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.method = method
