"""
AI Decision Engine (Stage 11) - оркестратор.

Собирает снапшоты из работающих движков (order_flow, liquidity, news),
извлекает метрики, прогоняет через Scoring Engine и возвращает финальный
вердикт ScoringResult (TRADE/NO_TRADE + direction + final_score + reason/warnings).

Полностью отказоустойчив: падение/неготовность любого движка превращает
его компонент в available=False (NA), но решение всё равно выносится по
доступным источникам (адаптивная схема B). Никогда не бросает наружу.

Singleton: init_ai_decision_engine() / get_ai_decision_engine().
"""
from __future__ import annotations
import logging
from typing import Optional

from app.config import get_settings
from app.engines.scoring import scoring as sc
from app.engines.scoring.models import ScoringResult, ScoreComponent

logger = logging.getLogger(__name__)


class AIDecisionEngine:
    """Оркестратор: снапшоты движков -> scoring -> вердикт."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def _news_mood(self) -> Optional[float]:
        """Средний sentiment свежих новостей [-1..1], или None."""
        try:
            from app.engines.news.engine import get_news_engine
            limit = self._settings.news_signal_mood_items
            snap = await get_news_engine().get_snapshot(limit=limit)
            if not snap.data_available or not snap.items:
                return None
            scores = [it.sentiment_score for it in snap.items
                      if it.sentiment_score is not None]
            if not scores:
                return None
            return sum(scores) / len(scores)
        except Exception as e:  # noqa: BLE001
            logger.debug("news mood unavailable: %s", e)
            return None

    async def _orderflow_components(self, symbol: str) -> list[ScoreComponent]:
        """delta + imbalance + volume из order_flow snapshot."""
        s = self._settings
        try:
            from app.engines.order_flow.engine import get_order_flow_engine
            of = await get_order_flow_engine().get_snapshot(symbol)
            if not of.data_available:
                raise RuntimeError("order_flow data not available")
            return [
                sc.score_delta(of.delta_1m, s.signal_cvd_min_abs_value),
                sc.score_imbalance(of.obi_top10, s.signal_obi_threshold),
                sc.score_volume(of.large_trade_count_1m, of.buy_aggression_1m,
                                s.signal_large_trade_min_count),
            ]
        except Exception as e:  # noqa: BLE001
            logger.debug("orderflow unavailable for %s: %s", symbol, e)
            # три компонента, помеченные недоступными
            return [
                ScoreComponent(name="delta", weight=sc.W_DELTA, available=False, note="orderflow NA"),
                ScoreComponent(name="imbalance", weight=sc.W_IMBALANCE, available=False, note="orderflow NA"),
                ScoreComponent(name="volume", weight=sc.W_VOLUME, available=False, note="orderflow NA"),
            ]

    async def _liquidity_component(self, symbol: str) -> ScoreComponent:
        """liquidity sweep proxy из liquidity snapshot."""
        try:
            from app.engines.liquidity.engine import get_liquidity_engine
            lq = await get_liquidity_engine().get_snapshot(symbol)
            if not lq.data_available:
                raise RuntimeError("liquidity data not available")
            return sc.score_liquidity(
                mid_price=lq.mid_price,
                local_low=lq.local_low,
                local_high=lq.local_high,
                has_zone_below=bool(lq.zones_below),
                has_zone_above=bool(lq.zones_above),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("liquidity unavailable for %s: %s", symbol, e)
            return ScoreComponent(name="liquidity", weight=sc.W_LIQUIDITY,
                                  available=False, note="liquidity NA")

    async def decide(self, symbol: str, tv_side: Optional[str] = None) -> ScoringResult:
        """Полный pipeline принятия решения по символу.

        tv_side: 'BUY'/'SELL' от TradingView webhook, или None.
        Возвращает ScoringResult. Никогда не бросает.
        """
        components: list[ScoreComponent] = []

        # order_flow: delta, imbalance, volume
        components.extend(await self._orderflow_components(symbol))
        # liquidity
        components.append(await self._liquidity_component(symbol))
        # news
        mood = await self._news_mood()
        components.append(sc.score_news(mood))
        # tradingview
        components.append(sc.score_tradingview(tv_side))
        # заглушки недоступных движков
        components.append(sc.score_trend_stub())
        components.append(sc.score_oi_stub())
        components.append(sc.score_social_stub())

        result = sc.aggregate(symbol, components)
        logger.info(
            "AI decision",
            extra={"symbol": symbol, "decision": result.decision,
                   "direction": result.direction, "score": result.final_score,
                   "confidence": result.confidence, "size": result.position_size,
                   "warnings": len(result.warnings)},
        )
        return result

    async def decide_and_log(self, symbol: str,
                             tv_side: Optional[str] = None) -> ScoringResult:
        """decide() + запись в журнал, если решение TRADE.

        Чистый decide() не трогает БД. Эта обёртка журналирует только
        TRADE-решения (Stage 11, компактный журнал). Сбой записи в БД
        не ломает результат — решение всегда возвращается.
        """
        result = await self.decide(symbol, tv_side=tv_side)
        if result.decision == "TRADE":
            try:
                from app.database.db import get_sessionmaker
                from app.database.trade_repository import TradeRepository
                sm = get_sessionmaker()
                async with sm() as session:
                    repo = TradeRepository(session)
                    await repo.save_ai_decision(
                        symbol=symbol,
                        decision=result.decision,
                        direction=result.direction,
                        confidence=result.confidence,
                        final_score=result.final_score,
                        components=[c.model_dump() for c in result.components],
                        full_response=result.model_dump(),
                    )
                logger.info("AI decision logged: %s %s score=%s",
                            symbol, result.direction, result.final_score)
            except Exception as e:  # noqa: BLE001
                logger.warning("failed to log AI decision for %s: %s", symbol, e)
        return result



_ai_engine: Optional[AIDecisionEngine] = None


def init_ai_decision_engine() -> AIDecisionEngine:
    """Создать singleton. Вызывается в lifespan после других движков."""
    global _ai_engine
    _ai_engine = AIDecisionEngine()
    logger.info("AI Decision Engine initialized")
    return _ai_engine


def get_ai_decision_engine() -> AIDecisionEngine:
    """Получить singleton (raises если не инициализирован)."""
    if _ai_engine is None:
        raise RuntimeError("Call init_ai_decision_engine() first")
    return _ai_engine
