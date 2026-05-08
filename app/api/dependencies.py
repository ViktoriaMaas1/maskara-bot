"""
FastAPI dependencies — переиспользуемые куски логики.

Назначение:
- request_id: каждый запрос получает UUID для трейсинга по логам
- settings: конфиг через DI (легче мокать в тестах)

В будущих этапах сюда добавим:
- get_db_session (Stage 11)
- get_redis (Шаг 1.7)
- get_current_user (если будем делать админку)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, Request

from app.config import Settings, get_settings


def get_request_id(
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> str:
    """
    Получает request_id из заголовка X-Request-ID или генерирует новый.

    Зачем: при отладке проблемной сделки можно по одному ID найти
    в логах все шаги — приём, валидацию, AI решение, ордер на бирже.
    """
    rid = x_request_id or str(uuid.uuid4())
    # Сохраняем в state — другие части обработки могут его использовать
    request.state.request_id = rid
    return rid


# Удобные type aliases для красивых сигнатур endpoint'ов
SettingsDep = Annotated[Settings, Depends(get_settings)]
RequestIdDep = Annotated[str, Depends(get_request_id)]
