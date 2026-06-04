"""SignalGenerator — главный класс Signal Generator (Stage 8).

Объединяет правила, проверяет конфликты, считает strength/score,
проверяет cooldown, сохраняет сигнал в Postgres и уведомляет в Telegram.

Pipeline (метод process_snapshot):
    1. Прогоняем OrderFlowSnapshot через ALL_RULES
    2. Если ни одно правило не сработало → None
    3. Если конфликт BUY vs SELL → None
    4. Считаем strength по числу сработавших правил
    5. Проверяем cooldown — если cooldown активен → None
    6. Ставим cooldown
    7. Сохраняем сигнал в Postgres (через сессию-фабрику)
    8. Уведомляем через Telegram (WEAK не шлём — это решает Notifier)
    9. Возвращаем итоговый Signal
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.engines.order_flow.models import OrderFlowSnapshot
from app.engines.signals.cooldown import CooldownGate
from app.engines.signals.models import Signal, SignalAction, SignalStrength
from app.engines.signals.notifier import SignalNotifier
from app.engines.signals.rules import ALL_RULES
from app.engines.signals.store import SignalStore
from app.config import get_settings
from app.engines.news.engine import get_news_engine

logger = logging.getLogger(__name__)


# ============================================================
# Маппинг: число сработавших правил → сила сигнала
# ============================================================

# 1 → WEAK, 2 → MEDIUM, 3+ → STRONG (см. STAGE_8_PLAN)
def _strength_for_count(count: int) -> SignalStrength:
    if count >= 3:
        return SignalStrength.STRONG
    if count == 2:
        return SignalStrength.MEDIUM
    return SignalStrength.WEAK


def _score_for_count(count: int, total_rules: int) -> float:
    """Score = доля сработавших правил, ограничено 1.0."""
    if total_rules <= 0:
        return 0.0
    return min(1.0, count / total_rules)


# Тип фабрики сессий — даёт async-контекст с AsyncSession внутри.
# В runtime это будет get_sessionmaker(), в тестах — фабрика на тестовой сессии.
SessionFactory = Callable[[], "AsyncContextManager"]  # noqa: F821 — упрощённая аннотация


class SignalGenerator:
    """Главный класс Signal Generator — full pipeline на один snapshot."""

    def __init__(
        self,
        session_factory: Callable,
        cooldown: CooldownGate,
        notifier: SignalNotifier,
    ) -> None:
        """
        Args:
            session_factory: callable, возвращающий async-context-manager
                             с AsyncSession внутри (обычно sessionmaker из db.py).
            cooldown: CooldownGate (Redis TTL).
            notifier: SignalNotifier (Telegram).
        """
        self._session_factory = session_factory
        self._cooldown = cooldown
        self._notifier = notifier

    # ============================================================
    # Главный метод
    # ============================================================

    async def process_snapshot(self, snapshot: OrderFlowSnapshot) -> Optional[Signal]:
        """Полный pipeline обработки одного snapshot'а.

        Возвращает итоговый Signal или None, если сигнала нет
        (правила не сработали, конфликт, или cooldown активен).
        """
        # 1. Прогон правил
        partials = self._run_rules(snapshot)
        if not partials:
            return None

        # 2. Объединение / конфликт-чек / strength
        final = self._combine(snapshot, partials)
        if final is None:
            return None

        # 2b. News sentiment influence (Stage 10 Phase 3): adjust score or veto.
        final = await self._apply_news_sentiment(final)
        if final is None:
            return None

        # 3. Cooldown check
        allowed = await self._cooldown.is_allowed(final.symbol, final.action.value)
        if not allowed:
            logger.info(
                "Сигнал заблокирован cooldown'ом",
                extra={
                    "symbol": final.symbol,
                    "action": final.action.value,
                    "strength": final.strength.value,
                },
            )
            return None

        # 4. Ставим cooldown ДО save — даже если что-то ниже упадёт,
        #    cooldown работает: спам в любом случае не пройдёт.
        await self._cooldown.mark_sent(final.symbol, final.action.value)

        # 5. Сохраняем в Postgres
        await self._save(final)

        # 6. Уведомляем в Telegram (Notifier сам решит, слать или нет)
        await self._notifier.notify(final)

        logger.info(
            "Signal сгенерирован",
            extra={
                "symbol": final.symbol,
                "action": final.action.value,
                "strength": final.strength.value,
                "score": final.score,
                "reasons_count": len(final.reasons),
            },
        )
        return final

    # ============================================================
    # Внутренние методы
    # ============================================================

    async def _apply_news_sentiment(self, signal: Signal) -> Optional[Signal]:
        """Stage 10 Phase 3: news sentiment influence on a signal (hybrid C).

        - Soft: nudge score by aligned_mood * weight.
        - Hard: veto the signal if aligned_mood <= veto_threshold.
        Aligned mood = mood for BUY, -mood for SELL (bullish news favors BUY).
        Fully fault-tolerant: any failure leaves the signal unchanged.
        Controlled by settings.news_signal_influence_enabled.
        """
        settings = get_settings()
        if not settings.news_signal_influence_enabled:
            return signal

        # Aggregate mood from freshest news; on any failure, pass signal through.
        try:
            snap = await get_news_engine().get_snapshot(limit=settings.news_signal_mood_items)
            if not snap.data_available or not snap.items:
                return signal
            scores = [it.sentiment_score for it in snap.items
                      if it.sentiment_score is not None]
            if not scores:
                return signal
            mood = sum(scores) / len(scores)
        except Exception as e:  # noqa: BLE001
            logger.warning("News mood unavailable - signal unchanged",
                           extra={"error": str(e)})
            return signal

        aligned = mood if signal.action == SignalAction.BUY else -mood
        signal.news_mood = round(mood, 4)

        # Hard veto
        if aligned <= settings.news_signal_veto_threshold:
            logger.info(
                "Signal VETOED by news sentiment",
                extra={"symbol": signal.symbol, "action": signal.action.value,
                       "mood": round(mood, 3), "aligned": round(aligned, 3),
                       "threshold": settings.news_signal_veto_threshold},
            )
            return None

        # Soft adjustment
        before = signal.score
        delta = aligned * settings.news_signal_score_weight
        signal.score = max(0.0, min(1.0, before + delta))
        signal.news_score_adjustment = round(signal.score - before, 4)
        logger.info(
            "News sentiment adjusted signal score",
            extra={"symbol": signal.symbol, "action": signal.action.value,
                   "mood": round(mood, 3), "score_before": round(before, 3),
                   "score_after": round(signal.score, 3),
                   "delta": signal.news_score_adjustment},
        )
        return signal

    def _run_rules(self, snapshot: OrderFlowSnapshot) -> list[Signal]:
        """Прогнать все правила, собрать non-None результаты."""
        partials: list[Signal] = []
        for rule in ALL_RULES:
            try:
                result = rule(snapshot)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Правило упало с исключением — пропускаем",
                    extra={"rule": rule.__name__, "error": str(e)},
                )
                continue
            if result is not None:
                partials.append(result)
        return partials

    def _combine(
        self,
        snapshot: OrderFlowSnapshot,
        partials: list[Signal],
    ) -> Optional[Signal]:
        """Объединить частичные сигналы в один итоговый.

        Возвращает None если конфликт BUY vs SELL.
        """
        buys = [p for p in partials if p.action == SignalAction.BUY]
        sells = [p for p in partials if p.action == SignalAction.SELL]

        # Конфликт — часть правил кричит BUY, часть SELL → не сигналим
        if buys and sells:
            logger.debug(
                "Конфликт сигналов — есть и BUY, и SELL правила, пропускаем",
                extra={
                    "symbol": snapshot.symbol,
                    "buy_rules": [p.reasons[0] for p in buys],
                    "sell_rules": [p.reasons[0] for p in sells],
                },
            )
            return None

        # Одна сторона: BUY или SELL
        winning = buys if buys else sells
        action = SignalAction.BUY if buys else SignalAction.SELL

        count = len(winning)
        strength = _strength_for_count(count)
        score = _score_for_count(count, total_rules=len(ALL_RULES))

        # Собираем reasons из всех частичных
        reasons: list[str] = []
        for p in winning:
            reasons.extend(p.reasons)

        # snapshot берём из первого partial (они все одинаковые)
        return Signal(
            symbol=snapshot.symbol,
            timestamp_ms=snapshot.timestamp_ms,
            action=action,
            strength=strength,
            score=score,
            reasons=reasons,
            snapshot=dict(winning[0].snapshot),
        )

    async def _save(self, signal: Signal) -> None:
        """Сохранить сигнал в Postgres через session_factory.

        Открывает свою сессию, делает commit. Не подымает исключение наружу,
        логирует ошибку — основной pipeline должен продолжить работу.
        """
        try:
            async with self._session_factory() as session:
                store = SignalStore(session)
                await store.save(signal)
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Не удалось сохранить сигнал в БД",
                extra={
                    "symbol": signal.symbol,
                    "action": signal.action.value,
                    "error": str(e),
                },
            )
			