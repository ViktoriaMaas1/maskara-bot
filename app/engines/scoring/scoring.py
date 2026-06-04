"""
Scoring Engine (Stage 11) - чистые функции начисления баллов + агрегатор.

Адаптивная схема (B): баллы считаются только от ДОСТУПНЫХ источников,
final_score нормализуется к 100 по сумме весов доступных. Отсутствующие
движки (trend/oi/social) дают available=False и не топят score.

Все функции чистые (без I/O), легко тестируются. Источники данных
(order_flow / liquidity / news snapshots) передаёт оркестратор
(AIDecisionEngine).
"""
from __future__ import annotations
from typing import List, Optional

from .models import (
    ScoreComponent, ScoringResult,
    W_TRADINGVIEW, W_LIQUIDITY, W_DELTA, W_IMBALANCE, W_VOLUME,
    W_TREND, W_OI, W_NEWS, W_SOCIAL, LONG, SHORT,
)


# ---------- источники баллов ----------

def score_delta(delta_1m: float, min_abs: float) -> ScoreComponent:
    """Delta confirmation (+15). Направление по знаку, сила по |delta|/3*порог."""
    c = ScoreComponent(name="delta", weight=W_DELTA, available=True)
    if abs(delta_1m) < min_abs:
        c.note = "delta below threshold"
        return c
    c.direction = LONG if delta_1m > 0 else SHORT
    strength = min(1.0, abs(delta_1m) / (3 * min_abs)) if min_abs > 0 else 1.0
    c.points = round(W_DELTA * strength, 2)
    c.note = f"delta_1m={delta_1m:.2f}"
    return c


def score_imbalance(obi_top10: float, threshold: float) -> ScoreComponent:
    """Orderbook imbalance (+15). obi in [-1,1], порог из конфига."""
    c = ScoreComponent(name="imbalance", weight=W_IMBALANCE, available=True)
    if abs(obi_top10) < threshold:
        c.note = "obi below threshold"
        return c
    c.direction = LONG if obi_top10 > 0 else SHORT
    denom = (1.0 - threshold) if threshold < 1.0 else 1.0
    strength = min(1.0, (abs(obi_top10) - threshold) / denom)
    c.points = round(W_IMBALANCE * (0.5 + 0.5 * strength), 2)
    c.note = f"obi_top10={obi_top10:.2f}"
    return c


def score_volume(large_trade_count: int, buy_aggression: float,
                 min_count: int) -> ScoreComponent:
    """Volume spike (+10). Всплеск крупных сделок; направление по агрессии."""
    c = ScoreComponent(name="volume", weight=W_VOLUME, available=True)
    if large_trade_count < min_count:
        c.note = "no volume spike"
        return c
    if buy_aggression > 0.55:
        c.direction = LONG
    elif buy_aggression < 0.45:
        c.direction = SHORT
    strength = min(1.0, large_trade_count / (2 * min_count)) if min_count > 0 else 1.0
    c.points = round(W_VOLUME * strength, 2)
    c.note = f"large_trades={large_trade_count}, aggr={buy_aggression:.2f}"
    return c


def score_liquidity(mid_price: float, local_low: Optional[float],
                    local_high: Optional[float], has_zone_below: bool,
                    has_zone_above: bool) -> ScoreComponent:
    """Liquidity sweep proxy (+20). Близость к sweep-уровню + крупная зона."""
    c = ScoreComponent(name="liquidity", weight=W_LIQUIDITY, available=True)
    if mid_price <= 0:
        c.available = False
        c.note = "no price"
        return c
    near_low = local_low is not None and abs(mid_price - local_low) / mid_price < 0.003
    near_high = local_high is not None and abs(mid_price - local_high) / mid_price < 0.003
    if near_low and has_zone_below:
        c.direction = LONG
        c.points = round(W_LIQUIDITY * 0.75, 2)
        c.note = "near local_low + bid wall"
    elif near_high and has_zone_above:
        c.direction = SHORT
        c.points = round(W_LIQUIDITY * 0.75, 2)
        c.note = "near local_high + ask wall"
    else:
        c.note = "no sweep proximity"
    return c


def score_news(mood: Optional[float]) -> ScoreComponent:
    """News confirmation (+10). mood in [-1,1] из news sentiment (Phase 2/3)."""
    c = ScoreComponent(name="news", weight=W_NEWS, available=(mood is not None))
    if mood is None:
        c.note = "no news data"
        return c
    if abs(mood) < 0.15:
        c.note = "news neutral"
        return c
    c.direction = LONG if mood > 0 else SHORT
    c.points = round(W_NEWS * min(1.0, abs(mood)), 2)
    c.note = f"mood={mood:+.2f}"
    return c


def score_tradingview(tv_side: Optional[str]) -> ScoreComponent:
    """TradingView signal (+20). Сигнал есть = полный вес ("запускатель")."""
    c = ScoreComponent(name="tradingview", weight=W_TRADINGVIEW,
                       available=(tv_side is not None))
    if tv_side is None:
        c.note = "no TV signal"
        return c
    c.direction = LONG if tv_side.upper() in ("BUY", "LONG") else SHORT
    c.points = float(W_TRADINGVIEW)
    c.note = f"TV={tv_side}"
    return c


# заглушки недоступных движков (Market Structure / External / Social)
def score_trend_stub() -> ScoreComponent:
    return ScoreComponent(name="trend", weight=W_TREND, available=False,
                          note="market_structure not built")


def score_oi_stub() -> ScoreComponent:
    return ScoreComponent(name="oi_funding", weight=W_OI, available=False,
                          note="external data not built")


def score_social_stub() -> ScoreComponent:
    return ScoreComponent(name="social", weight=W_SOCIAL, available=False,
                          note="social engine not built")


# ---------- агрегатор ----------

def aggregate(symbol: str, components: List[ScoreComponent]) -> ScoringResult:
    """Свести компоненты в вердикт по правилам спеки.

    - final_score = winning_points / sum(weights of available) * 100
    - <70 NO_TRADE; 70-84 small; 85+ normal
    - конфликт направлений -> warning (и NO_TRADE если score<70)
    """
    res = ScoringResult(symbol=symbol, components=components)
    avail = [c for c in components if c.available]

    if not avail:
        res.reason = ["no data sources available"]
        res.warnings = ["all engines unavailable"]
        return res

    long_pts = sum(c.points for c in avail if c.direction == LONG)
    short_pts = sum(c.points for c in avail if c.direction == SHORT)

    if long_pts == 0 and short_pts == 0:
        res.reason = ["no directional signal"]
        return res

    direction = LONG if long_pts >= short_pts else SHORT
    winning = long_pts if direction == LONG else short_pts
    losing = short_pts if direction == LONG else long_pts

    if losing > 0 and losing >= 0.6 * winning:
        res.warnings.append(
            f"directional conflict (LONG={long_pts:.1f} SHORT={short_pts:.1f})"
        )

    avail_weight = sum(c.weight for c in avail)
    res.final_score = round((winning / avail_weight) * 100, 1) if avail_weight else 0.0

    for c in avail:
        if c.points > 0 and c.direction == direction:
            res.reason.append(f"{c.name}: {c.note} (+{c.points})")

    # confidence
    if res.final_score >= 85:
        res.confidence = "HIGH"
    elif res.final_score >= 70:
        res.confidence = "MEDIUM"
    else:
        res.confidence = "LOW"

    # decision + position size (правила спеки)
    if res.final_score < 70 or res.warnings:
        res.decision = "NO_TRADE"
        res.direction = None
        res.position_size = "none"
    else:
        res.decision = "TRADE"
        res.direction = direction
        res.position_size = "normal" if res.final_score >= 85 else "small"

    return res
