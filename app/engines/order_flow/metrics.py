"""
Чистые функции для расчёта метрик order flow.

Все функции stateless и детерминированы: при одинаковых входных данных
дают одинаковый результат. Это критично для тестируемости.

Битые trade (price=None, qty='abc') отбрасываются с логированием —
бот не падает, но в логах видно проблему. Если входные данные пустые,
метрики возвращают нейтральный 0.0 (флаг data_available в Snapshot
сигнализирует Signal Engine, что данных нет).

Bybit формат:
- trade: {ts: int_ms, side: "Buy"|"Sell", price: str, qty: str, tradeId: str}
- orderbook: {b: [[price_str, qty_str], ...], a: [[...]], ts, u, seq}
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Хелперы
# ============================================================

def _parse_float(value: Any) -> Optional[float]:
    """Безопасное преобразование в float.

    Возвращает None для невалидных значений (None, '', 'abc', и т.п.).
    Используется для парсинга строковых полей Bybit (price, qty).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _filter_trades_by_window(
    trades: list[dict],
    window_seconds: int,
    now_ms: int,
) -> list[dict]:
    """Оставляет только trade, попавшие в окно [now_ms - window, now_ms].

    Старые trade отбрасываются. Окно полузакрытое: ts >= cutoff_ms.
    """
    cutoff_ms = now_ms - window_seconds * 1000
    return [t for t in trades if isinstance(t, dict) and t.get("ts", 0) >= cutoff_ms]


def _split_buy_sell_volume(trades: list[dict]) -> tuple[float, float, int]:
    """Считает суммарный объём покупок/продаж по списку trades.

    Возвращает (buy_volume, sell_volume, skipped_count).
    Битые записи отбрасываются и логируются.
    """
    buy_volume = 0.0
    sell_volume = 0.0
    skipped = 0

    for t in trades:
        qty = _parse_float(t.get("qty"))
        side = t.get("side")

        if qty is None or qty < 0 or side not in ("Buy", "Sell"):
            skipped += 1
            continue

        if side == "Buy":
            buy_volume += qty
        else:
            sell_volume += qty

    if skipped > 0:
        logger.warning(
            "Order flow: skipped %d malformed trades (qty/side invalid)", skipped
        )

    return buy_volume, sell_volume, skipped


# ============================================================
# Публичные метрики
# ============================================================

def compute_delta(
    trades: list[dict],
    window_seconds: int,
    now_ms: int,
) -> float:
    """Дельта = buy_volume - sell_volume за окно.

    >0 -> доминируют агрессивные покупатели.
    <0 -> доминируют агрессивные продавцы.
    Если trade нет -> 0.0.
    """
    if not trades:
        return 0.0

    windowed = _filter_trades_by_window(trades, window_seconds, now_ms)
    buy_vol, sell_vol, _ = _split_buy_sell_volume(windowed)
    return buy_vol - sell_vol


def compute_tfi(
    trades: list[dict],
    window_seconds: int,
    now_ms: int,
) -> float:
    """Trade Flow Imbalance.

    TFI = (buy_volume - sell_volume) / (buy_volume + sell_volume)
    Диапазон: [-1, +1].
    Если total_volume == 0 -> 0.0 (нейтрально).
    """
    if not trades:
        return 0.0

    windowed = _filter_trades_by_window(trades, window_seconds, now_ms)
    buy_vol, sell_vol, _ = _split_buy_sell_volume(windowed)
    total = buy_vol + sell_vol

    if total <= 0.0:
        return 0.0

    return (buy_vol - sell_vol) / total


def compute_aggression(
    trades: list[dict],
    window_seconds: int,
    now_ms: int,
) -> dict[str, float]:
    """Доля покупателей-агрессоров и общий объём за окно.

    Возвращает {
        "buy_aggression": float,  # buy_volume / total_volume, [0, 1]
        "total_volume": float,    # buy + sell за окно
        "trades_count": int,      # сколько валидных trade в окне
    }
    Если total_volume == 0 -> buy_aggression = 0.0.
    """
    if not trades:
        return {"buy_aggression": 0.0, "total_volume": 0.0, "trades_count": 0}

    windowed = _filter_trades_by_window(trades, window_seconds, now_ms)
    buy_vol, sell_vol, skipped = _split_buy_sell_volume(windowed)
    total = buy_vol + sell_vol
    valid_count = len(windowed) - skipped

    if total <= 0.0:
        return {
            "buy_aggression": 0.0,
            "total_volume": 0.0,
            "trades_count": valid_count,
        }

    return {
        "buy_aggression": buy_vol / total,
        "total_volume": total,
        "trades_count": valid_count,
    }


def compute_obi(orderbook: Optional[dict], depth: int) -> float:
    """Order Book Imbalance на топ-N уровнях стакана.

    OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)
    Диапазон: [-1, +1].
    >0 -> биды толще (поддержка), <0 -> аски толще (сопротивление).

    Если orderbook=None или пустой -> 0.0.
    Битые уровни отбрасываются.
    """
    if not orderbook:
        return 0.0

    bids_raw = orderbook.get("b") or []
    asks_raw = orderbook.get("a") or []

    bid_volume = 0.0
    for level in bids_raw[:depth]:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        qty = _parse_float(level[1])
        if qty is not None and qty >= 0:
            bid_volume += qty

    ask_volume = 0.0
    for level in asks_raw[:depth]:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        qty = _parse_float(level[1])
        if qty is not None and qty >= 0:
            ask_volume += qty

    total = bid_volume + ask_volume
    if total <= 0.0:
        return 0.0

    return (bid_volume - ask_volume) / total


def detect_large_trades(
    trades: list[dict],
    window_seconds: int,
    now_ms: int,
    percentile: float = 95.0,
) -> int:
    """Кол-во крупных сделок (qty выше N-го перцентиля) за окно.

    Перцентиль считается по самому окну (адаптивный порог).
    Требуется минимум 10 валидных trade — иначе вернёт 0.
    Это защита от ложных срабатываний на тонком рынке.
    """
    if not trades:
        return 0

    windowed = _filter_trades_by_window(trades, window_seconds, now_ms)

    qtys = []
    for t in windowed:
        q = _parse_float(t.get("qty"))
        if q is not None and q >= 0:
            qtys.append(q)

    if len(qtys) < 10:
        return 0

    qtys_sorted = sorted(qtys)
    # Индекс перцентиля. percentile=95 -> 95% наименьших, остальные 5% = крупные
    idx = int(len(qtys_sorted) * (percentile / 100.0))
    if idx >= len(qtys_sorted):
        idx = len(qtys_sorted) - 1

    threshold = qtys_sorted[idx]
    return sum(1 for q in qtys if q > threshold)