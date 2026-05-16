"""
Общие фикстуры для тестов Order Flow Engine.

now_ms - фиксированный момент времени для детерминированных тестов.
trades - наборы синтетических сделок (структура Bybit-format).
orderbook - наборы стаканов.
"""

from __future__ import annotations

import pytest


# ============================================================
# Время
# ============================================================

@pytest.fixture
def now_ms() -> int:
    """Фиксированный момент времени (mid-2026)."""
    return 1778850000000


# ============================================================
# Trades
# ============================================================

@pytest.fixture
def simple_trades(now_ms: int) -> list[dict]:
    """3 trade: 2 Buy (3.0) и 1 Sell (0.5). Все в пределах 60с."""
    return [
        {"ts": now_ms - 10_000, "side": "Buy", "price": "80000", "qty": "1.0", "tradeId": "t1"},
        {"ts": now_ms - 20_000, "side": "Sell", "price": "80000", "qty": "0.5", "tradeId": "t2"},
        {"ts": now_ms - 30_000, "side": "Buy", "price": "80000", "qty": "2.0", "tradeId": "t3"},
    ]


@pytest.fixture
def balanced_trades(now_ms: int) -> list[dict]:
    """Buy == Sell по объёму. delta = 0, tfi = 0."""
    return [
        {"ts": now_ms - 5_000, "side": "Buy", "price": "80000", "qty": "1.0", "tradeId": "b1"},
        {"ts": now_ms - 6_000, "side": "Sell", "price": "80000", "qty": "1.0", "tradeId": "b2"},
    ]


@pytest.fixture
def only_buy_trades(now_ms: int) -> list[dict]:
    """Все агрессивные покупки. tfi = 1.0, buy_aggression = 1.0."""
    return [
        {"ts": now_ms - 1_000, "side": "Buy", "price": "80000", "qty": "0.5", "tradeId": "p1"},
        {"ts": now_ms - 2_000, "side": "Buy", "price": "80000", "qty": "1.5", "tradeId": "p2"},
    ]


@pytest.fixture
def trades_with_old(now_ms: int) -> list[dict]:
    """1 свежий trade и 2 старых (вне окна 60с)."""
    return [
        {"ts": now_ms - 5_000, "side": "Buy", "price": "80000", "qty": "1.0", "tradeId": "f1"},
        {"ts": now_ms - 300_000, "side": "Sell", "price": "80000", "qty": "10.0", "tradeId": "o1"},
        {"ts": now_ms - 600_000, "side": "Buy", "price": "80000", "qty": "20.0", "tradeId": "o2"},
    ]


@pytest.fixture
def trades_with_garbage(now_ms: int) -> list[dict]:
    """Смесь валидных и битых trade."""
    return [
        {"ts": now_ms - 1_000, "side": "Buy", "price": "80000", "qty": "1.0", "tradeId": "v1"},
        {"ts": now_ms - 2_000, "side": "Sell", "price": "80000", "qty": "0.5", "tradeId": "v2"},
        {"ts": now_ms - 3_000, "side": "Buy", "price": "80000", "qty": "abc", "tradeId": "g1"},
        {"ts": now_ms - 4_000, "side": "Sell", "price": "80000", "qty": None, "tradeId": "g2"},
        {"ts": now_ms - 5_000, "side": "Unknown", "price": "80000", "qty": "1.0", "tradeId": "g3"},
    ]


@pytest.fixture
def many_trades_with_whale(now_ms: int) -> list[dict]:
    """15 trade: 14 мелких (qty=0.1) + 1 крупный (qty=10.0)."""
    base = [
        {
            "ts": now_ms - (i + 1) * 1_000,
            "side": "Buy" if i % 2 == 0 else "Sell",
            "price": "80000",
            "qty": "0.1",
            "tradeId": f"small_{i}",
        }
        for i in range(14)
    ]
    whale = {
        "ts": now_ms - 500,
        "side": "Buy",
        "price": "80000",
        "qty": "10.0",
        "tradeId": "whale",
    }
    return base + [whale]


@pytest.fixture
def many_uniform_trades(now_ms: int) -> list[dict]:
    """15 одинаковых trade. Перцентиль не найдёт "кита"."""
    return [
        {
            "ts": now_ms - (i + 1) * 1_000,
            "side": "Buy",
            "price": "80000",
            "qty": "1.0",
            "tradeId": f"u_{i}",
        }
        for i in range(15)
    ]


# ============================================================
# Orderbook
# ============================================================

@pytest.fixture
def simple_orderbook() -> dict:
    """Bids толще asks. OBI top-5 положительный."""
    return {
        "b": [["80000", "1.0"], ["79999", "1.0"], ["79998", "1.0"],
              ["79997", "1.0"], ["79996", "1.0"]],
        "a": [["80001", "0.5"], ["80002", "0.5"], ["80003", "0.5"],
              ["80004", "0.5"], ["80005", "0.5"]],
        "ts": 1778850000000,
        "u": 1,
        "seq": 1,
    }


@pytest.fixture
def deep_orderbook() -> dict:
    """20 уровней. Bids толще на top-5, на глубине asks толще."""
    bids = [["80000", "2.0"]] * 5 + [["79995", "0.5"]] * 15
    asks = [["80001", "0.5"]] * 5 + [["80006", "2.0"]] * 15
    return {
        "b": bids,
        "a": asks,
        "ts": 1778850000000,
        "u": 1,
        "seq": 1,
    }


@pytest.fixture
def garbage_orderbook() -> dict:
    """Стакан с битыми уровнями."""
    return {
        "b": [
            ["80000", "1.0"],
            ["79999", "abc"],
            ["79998"],
            None,
            ["79997", "0.5"],
        ],
        "a": [
            ["80001", "0.5"],
            "not a list",
            ["80002", "0.5"],
        ],
        "ts": 1778850000000,
        "u": 1,
        "seq": 1,
    }