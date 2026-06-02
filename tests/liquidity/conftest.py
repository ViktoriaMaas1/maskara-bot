"""
Фикстуры для тестов Liquidity Engine.

Реальные форматы данных из market_cache.
"""

import pytest


@pytest.fixture
def sample_orderbook_with_walls():
    """Orderbook с крупными стенками выше/ниже цены."""
    return {
        "b": [  # Bids (покупки) — ниже цены
            ["69000.00", "5.0"],   # Крупная стенка
            ["68900.00", "1.2"],
            ["68800.00", "0.8"],
            ["68700.00", "1.5"],
        ],
        "a": [  # Asks (продажи) — выше цены
            ["69500.00", "6.0"],   # Крупная стенка
            ["69600.00", "1.1"],
            ["69700.00", "0.9"],
            ["69800.00", "1.4"],
        ],
    }


@pytest.fixture
def sample_orderbook_empty():
    """Пустой orderbook."""
    return {"b": [], "a": []}


@pytest.fixture
def sample_trades_with_extremes():
    """Список сделок с чёткими макс/мин."""
    return [
        {"ts": 1780398900000, "side": "Buy", "price": "69400.00", "qty": "0.5"},
        {"ts": 1780398895000, "side": "Sell", "price": "69200.00", "qty": "1.2"},
        {"ts": 1780398890000, "side": "Buy", "price": "69350.00", "qty": "0.8"},
        {"ts": 1780398885000, "side": "Sell", "price": "69100.00", "qty": "2.0"},  # MIN
        {"ts": 1780398880000, "side": "Buy", "price": "69500.00", "qty": "1.5"},   # MAX
    ]


@pytest.fixture
def sample_liquidations_valid():
    """Список ликвидаций с валидными данными."""
    return [
        {"ts": 1780398900000, "side": "Buy", "price": "69000.00", "qty": "0.5"},
        {"ts": 1780398895000, "side": "Sell", "price": "69200.00", "qty": "1.0"},
        {"ts": 1780398890000, "side": "Buy", "price": "69100.00", "qty": "2.0"},
    ]


@pytest.fixture
def sample_liquidations_with_junk():
    """Список ликвидаций с мусором (нулевые цены, пустые side)."""
    return [
        {"ts": 1780398900000, "side": "Buy", "price": "69000.00", "qty": "0.5"},
        {"ts": 1780398895000, "side": "", "price": "69200.00", "qty": "1.0"},       # Мусор: пусто side
        {"ts": 1780398890000, "side": "Buy", "price": "0", "qty": "2.0"},          # Мусор: price = 0
        {"ts": 1780398885000, "side": "Sell", "price": "69100.00", "qty": "1.5"},
    ]


@pytest.fixture
def sample_liquidations_empty():
    """Пустой список ликвидаций."""
    return []
