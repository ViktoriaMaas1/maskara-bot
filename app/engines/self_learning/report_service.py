"""
Self-Learning AI — Stage 12: report service
============================================
Тонкая асинхронная обёртка между журналом БД и чистым analyzer.

Открывает сессию через get_sessionmaker (тот же паттерн, что и
эндпоинты дашборда), тянет последние решения AI и количество
закрытых сделок, и зовёт analyzer.build_report().

Здесь нет бизнес-логики — только сбор данных. Весь анализ в
analyzer.py (чистые функции, без БД). Это разделение делает
analyzer тестируемым в песочнице, а сервис — тонким и безопасным.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.database.db import get_sessionmaker
from app.database.models import Trade
from app.database.trade_repository import TradeRepository
from app.engines.self_learning.analyzer import build_report


async def count_closed_trades(session) -> int:
    """Сколько сделок со status='closed' (для gating-дисклеймера спеки)."""
    result = await session.execute(
        select(func.count()).select_from(Trade).where(Trade.status == "closed")
    )
    return int(result.scalar() or 0)


async def build_decision_report(limit: int = 200) -> dict:
    """Собрать Self-Learning отчёт по журналу решений.

    limit — сколько последних решений анализировать.
    Возвращает dict от analyzer.build_report (+ analyzed_limit),
    приведённый к JSON-безопасному виду.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        repo = TradeRepository(session)
        rows = await repo.get_recent_ai_decisions(limit=limit)
        closed = await count_closed_trades(session)

    report = build_report(rows, closed_trades=closed)

    # JSON-безопасность: ключи time_of_day -> строки (часы int -> str)
    if isinstance(report.get("time_of_day"), dict):
        report["time_of_day"] = {str(k): v for k, v in report["time_of_day"].items()}

    report["analyzed_limit"] = limit
    return report
