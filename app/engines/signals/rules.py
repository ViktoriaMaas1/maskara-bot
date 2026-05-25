from __future__ import annotations

from typing import Optional

from app.engines.order_flow.models import OrderFlowSnapshot
from app.engines.signals.models import Signal, SignalAction, SignalStrength


# ============================================================
# Константы по умолчанию (из STAGE_8_PLAN.md)
# Реальные значения подставит SignalGenerator из .env
# ============================================================

DEFAULT_OBI_THRESHOLD = 0.7
DEFAULT_AGGRESSION_THRESHOLD = 0.65
DEFAULT_CVD_MIN_ABS_VALUE = 1.0
DEFAULT_LARGE_TRADE_MIN_COUNT = 5
DEFAULT_TFI_LARGE_THRESHOLD = 0.5
DEFAULT_TFI_5M_EXHAUSTION = 0.6
DEFAULT_TFI_30S_REVERSAL = 0.3

# Каждое правило вносит 0.25 в итоговый score (4 правила суммарно = 1.0)
RULE_SCORE_CONTRIBUTION = 0.25


# ============================================================
# Вспомогательные функции
# ============================================================

def _build_partial_signal(
    snapshot: OrderFlowSnapshot,
    action: SignalAction,
    rule_name: str,
) -> Signal:
    """Собрать частичный Signal, который вернёт правило.

    SignalGenerator потом объединит частичные сигналы от разных правил
    и пересчитает strength/score на основе их количества.
    """
    return Signal(
        symbol=snapshot.symbol,
        timestamp_ms=snapshot.timestamp_ms,
        action=action,
        strength=SignalStrength.WEAK,  # промежуточное, Generator пересчитает
        score=RULE_SCORE_CONTRIBUTION,  # промежуточное, Generator пересуммирует
        reasons=[rule_name],
        snapshot={
            "obi_top10": snapshot.obi_top10,
            "buy_aggression_1m": snapshot.buy_aggression_1m,
            "cvd": snapshot.cvd,
            "delta_30s": snapshot.delta_30s,
            "tfi_30s": snapshot.tfi_30s,
            "tfi_1m": snapshot.tfi_1m,
            "tfi_5m": snapshot.tfi_5m,
            "large_trade_count_1m": snapshot.large_trade_count_1m,
        },
    )


# ============================================================
# Правило 1: Сильный дисбаланс orderbook + агрессия покупателей/продавцов
# ============================================================

def rule_orderbook_imbalance(
    snapshot: OrderFlowSnapshot,
    obi_threshold: float = DEFAULT_OBI_THRESHOLD,
    aggression_threshold: float = DEFAULT_AGGRESSION_THRESHOLD,
) -> Optional[Signal]:
    """BUY  если obi_top10 > obi_threshold И buy_aggression_1m > aggression_threshold.
    SELL если obi_top10 < -obi_threshold И buy_aggression_1m < (1 - aggression_threshold).
    """
    if not snapshot.data_available:
        return None

    # BUY: толстая стена бидов + покупатели агрессивны
    if (
        snapshot.obi_top10 > obi_threshold
        and snapshot.buy_aggression_1m > aggression_threshold
    ):
        return _build_partial_signal(snapshot, SignalAction.BUY, "orderbook_imbalance")

    # SELL: толстая стена асков + продавцы агрессивны
    sell_aggression_threshold = 1.0 - aggression_threshold
    if (
        snapshot.obi_top10 < -obi_threshold
        and snapshot.buy_aggression_1m < sell_aggression_threshold
    ):
        return _build_partial_signal(snapshot, SignalAction.SELL, "orderbook_imbalance")

    return None


# ============================================================
# Правило 2: CVD-разворот (тренд накопленной дельты + текущий импульс)
# ============================================================

def rule_cvd_trend(
    snapshot: OrderFlowSnapshot,
    cvd_min_abs_value: float = DEFAULT_CVD_MIN_ABS_VALUE,
) -> Optional[Signal]:
    """BUY  если cvd > cvd_min_abs_value И delta_30s > 0.
    SELL если cvd < -cvd_min_abs_value И delta_30s < 0.
    """
    if not snapshot.data_available:
        return None

    # BUY: накопленный покупательский тренд + текущий импульс вверх
    if snapshot.cvd > cvd_min_abs_value and snapshot.delta_30s > 0:
        return _build_partial_signal(snapshot, SignalAction.BUY, "cvd_trend")

    # SELL: накопленный продавательский тренд + текущий импульс вниз
    if snapshot.cvd < -cvd_min_abs_value and snapshot.delta_30s < 0:
        return _build_partial_signal(snapshot, SignalAction.SELL, "cvd_trend")

    return None


# ============================================================
# Правило 3: «Кит вошёл» (всплеск крупных сделок + однонаправленный TFI)
# ============================================================

def rule_large_trades(
    snapshot: OrderFlowSnapshot,
    large_trade_min_count: int = DEFAULT_LARGE_TRADE_MIN_COUNT,
    tfi_large_threshold: float = DEFAULT_TFI_LARGE_THRESHOLD,
) -> Optional[Signal]:
    """BUY  если large_trade_count_1m >= N И tfi_1m > +threshold.
    SELL если large_trade_count_1m >= N И tfi_1m < -threshold.
    """
    if not snapshot.data_available:
        return None

    if snapshot.large_trade_count_1m < large_trade_min_count:
        return None

    # BUY: киты покупают
    if snapshot.tfi_1m > tfi_large_threshold:
        return _build_partial_signal(snapshot, SignalAction.BUY, "large_trades")

    # SELL: киты продают
    if snapshot.tfi_1m < -tfi_large_threshold:
        return _build_partial_signal(snapshot, SignalAction.SELL, "large_trades")

    return None


# ============================================================
# Правило 4: Истощение тренда (длинный TFI вычерпан, краткосрочный развернулся)
# ============================================================

def rule_exhaustion(
    snapshot: OrderFlowSnapshot,
    tfi_5m_exhaustion: float = DEFAULT_TFI_5M_EXHAUSTION,
    tfi_30s_reversal: float = DEFAULT_TFI_30S_REVERSAL,
) -> Optional[Signal]:
    """BUY  если tfi_5m < -exhaustion И tfi_30s > +reversal  (медведи истощены).
    SELL если tfi_5m > +exhaustion И tfi_30s < -reversal  (быки истощены).
    """
    if not snapshot.data_available:
        return None

    # BUY: продавцы выдохлись, появляется покупательский импульс
    if (
        snapshot.tfi_5m < -tfi_5m_exhaustion
        and snapshot.tfi_30s > tfi_30s_reversal
    ):
        return _build_partial_signal(snapshot, SignalAction.BUY, "exhaustion")

    # SELL: покупатели выдохлись, появляется продавательский импульс
    if (
        snapshot.tfi_5m > tfi_5m_exhaustion
        and snapshot.tfi_30s < -tfi_30s_reversal
    ):
        return _build_partial_signal(snapshot, SignalAction.SELL, "exhaustion")

    return None


# ============================================================
# Реестр всех правил (для удобства SignalGenerator)
# ============================================================

ALL_RULES = [
    rule_orderbook_imbalance,
    rule_cvd_trend,
    rule_large_trades,
    rule_exhaustion,
]