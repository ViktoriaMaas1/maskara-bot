"""
Liquidity Detector — чистые функции анализа ликвидности.

Берут сырые данные из market_cache (orderbook, trades, liquidations)
и возвращают объекты LiquidityZone, локальные экстремумы, счётчики.
Без побочных эффектов — легко тестировать.
"""

from typing import List, Optional, Dict, Any

from .models import LiquidityZone, ZoneSide


# ====================================================================
# find_orderbook_walls — ищет крупные стенки в стакане
# ====================================================================

def find_orderbook_walls(
    orderbook: Dict[str, Any],
    mid_price: float,
    wall_percentile: float = 75.0,
) -> tuple[List[LiquidityZone], List[LiquidityZone]]:
    """Ищет уровни в stакане, где объём выше среднего (потенциальные стенки).

    Параметры:
        orderbook: {"b": [["69000", "1.5"], ...], "a": [["69500", "2.0"], ...]}
                   Bids (покупки) и asks (продажи). Цены и размеры — строки!
        mid_price: средняя цена (bid+ask)/2
        wall_percentile: уровень квантиля для определения "стенки" (default 75%)

    Возвращает:
        (zones_below, zones_above) — зоны ниже и выше цены, отсортированы
        по близости к текущей цене.
    """
    zones_below = []
    zones_above = []

    # Обработаем bids (ключ "b") — зоны ниже цены
    bids = orderbook.get("b", [])
    if bids:
        sizes = [float(level[1]) for level in bids]
        avg_size = sum(sizes) / len(sizes)
        threshold = avg_size  # Простой критерий: объём выше среднего

        for price_str, size_str in bids:
            price = float(price_str)
            size = float(size_str)
            if size > threshold and price < mid_price:
                distance_pct = ((mid_price - price) / mid_price) * 100
                zone = LiquidityZone(
                    price=price,
                    size=size,
                    side=ZoneSide.BELOW,
                    distance_pct=distance_pct,
                )
                zones_below.append(zone)

    # Обработаем asks (ключ "a") — зоны выше цены
    asks = orderbook.get("a", [])
    if asks:
        sizes = [float(level[1]) for level in asks]
        avg_size = sum(sizes) / len(sizes)
        threshold = avg_size

        for price_str, size_str in asks:
            price = float(price_str)
            size = float(size_str)
            if size > threshold and price > mid_price:
                distance_pct = ((price - mid_price) / mid_price) * 100
                zone = LiquidityZone(
                    price=price,
                    size=size,
                    side=ZoneSide.ABOVE,
                    distance_pct=distance_pct,
                )
                zones_above.append(zone)

    # Сортируем по близости к цене (возрастающее расстояние)
    zones_below.sort(key=lambda z: z.distance_pct, reverse=True)  # Ближайшие последние
    zones_above.sort(key=lambda z: z.distance_pct)

    return zones_below, zones_above


# ====================================================================
# detect_local_highs_lows — ищет локальные макс/мин из свежих сделок
# ====================================================================

def detect_local_highs_lows(
    trades: List[Dict[str, Any]],
    window_size: int = 50,
) -> tuple[Optional[float], Optional[float]]:
    """Находит локальный максимум и минимум цены за окно свежих сделок.

    Параметры:
        trades: [{"ts": 1780..., "price": "69384.30", ...}, ...]
                Список сделок от market_cache.get_trades() (новейшие первыми)
        window_size: сколько последних сделок смотрим (default 50)

    Возвращает:
        (local_high, local_low) или (None, None) если сделок нет
    """
    if not trades:
        return None, None

    # Берём не более window_size сделок
    window = trades[: min(window_size, len(trades))]
    prices = [float(t.get("price", 0)) for t in window]

    if not prices:
        return None, None

    return max(prices), min(prices)


# ====================================================================
# count_recent_liquidations — считает ликвидации в списке
# ====================================================================

def count_recent_liquidations(liquidations: List[Dict[str, Any]]) -> int:
    """Считает действительные ликвидации (отфильтровывает мусор).

    Параметры:
        liquidations: [{"ts": ..., "side": "Buy", "price": "69000", "qty": "0.5"}, ...]
                      Из market_cache.get_liquidations()

    Возвращает:
        Число валидных ликвидаций (где price > 0, side не пусто)
    """
    count = 0
    for liq in liquidations:
        price_str = liq.get("price", "0")
        side = liq.get("side", "")

        try:
            price = float(price_str)
            # Фильтруем мусор: невалидные цены (0 или отрицательные) и пустой side
            if price > 0 and side:
                count += 1
        except (ValueError, TypeError):
            # Если price не парсится — пропускаем
            continue

    return count
