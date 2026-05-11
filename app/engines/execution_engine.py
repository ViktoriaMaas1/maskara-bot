"""
Execution Engine — Stage 4.

Точка входа для торговых действий бота. Объединяет:
- Risk Manager (Stage 3) — pre-trade проверки и position sizing
- Bybit REST Client (Stage 2 + 4 расширения) — set_leverage, place_market_order
  с атомарными SL/TP, get_order_by_id
- TradeRepository (Stage 3 + 4 расширения) — create_open_trade, close_trade
- Telegram Notifier (Stage 4) — уведомления

Публичный API:
- open_position(...)       — полный цикл открытия позиции
- close_position(...)      — закрытие конкретной сделки
- emergency_close_all(...) — массовое закрытие (для kill switch)

Архитектура retry:
- Сетевые ошибки (BybitNetworkError, BybitRateLimitError) — ретраим с
  exponential backoff: settings.execution_retry_delay_sec * 2^n
- Логические ошибки (BybitAuthError, BybitReadOnlyError, RiskCheckFailed) —
  НЕ ретраим, кидаем дальше / уведомляем

TODO (следующие stage'и):
- Stage 5/6: snapshot_entry_id / snapshot_exit_id — для Trade Journal
- Stage 10: ai_decision_id — связь с AI Decision Engine
- Stage 11: расчёт fees/slippage точнее (сейчас приблизительно)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import Trade
from app.database.trade_repository import TradeRepository
from app.engines.risk_manager import RiskCheckFailed, risk_manager
from app.bybit.exceptions import (
    BybitAPIError,
    BybitAuthError,
    BybitNetworkError,
    BybitRateLimitError,
    BybitReadOnlyError,
)
from app.telegram.notifier import (
    notify_error,
    notify_trade_closed,
    notify_trade_opened,
)

logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class ExecutionError(Exception):
    """Базовая ошибка Execution Engine."""
    pass


class TradeNotFoundError(ExecutionError):
    """Сделка не найдена в БД или уже закрыта."""
    pass


# ============================================================
# Helpers
# ============================================================

def _normalize_side(side: str) -> str:
    """
    Привести side к Bybit-формату.

    Webhook от TradingView присылает "BUY"/"SELL" (uppercase),
    Bybit ожидает "Buy"/"Sell" (capitalized).
    """
    s = side.strip().lower()
    if s in ("buy", "long"):
        return "Buy"
    if s in ("sell", "short"):
        return "Sell"
    raise ValueError(f"Unknown side: {side!r}")


def _make_order_link_id(prefix: str) -> str:
    """
    Сгенерировать уникальный orderLinkId для Bybit.

    Формат: "<prefix>-<8 first chars of uuid4>"
    Например: "maskara-a3f1c8b2"

    Используется для:
    1. Идемпотентности — при повторе запроса Bybit вернёт ту же сделку,
       а не создаст новую
    2. Поиска наших ордеров в логах/dashboard Bybit
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _calc_pnl(side: str, entry: Decimal, exit_: Decimal, qty: Decimal) -> Decimal:
    """
    Расчёт PnL без учёта плеча (Bybit считает PnL в quote currency).

    Long:  (exit - entry) * qty
    Short: (entry - exit) * qty

    Это приблизительно — точный PnL придёт от Bybit через get_closed_pnl
    в Stage 11. Сейчас используется как fallback для close_trade.
    """
    if side == "Buy":
        return (exit_ - entry) * qty
    if side == "Sell":
        return (entry - exit_) * qty
    raise ValueError(f"Unknown side: {side!r}")


# ============================================================
# ExecutionEngine
# ============================================================

class ExecutionEngine:
    """
    Stage 4: оркестратор торговых действий.

    Используется как singleton: `from app.engines.execution_engine import execution_engine`.
    """

    async def open_position(
        self,
        *,
        symbol: str,
        side: str,                              # "BUY"/"SELL" или "Buy"/"Sell"
        sl_price: Decimal,
        tp_price: Decimal,
        ai_score: float,
        session: AsyncSession,
        signal_id: Optional[uuid.UUID] = None,
        timeframe: Optional[str] = None,
        size_factor: Decimal = Decimal("1.0"),
        entry_reason: Optional[str] = None,
    ) -> Trade:
        """
        Полный цикл открытия позиции.

        Порядок:
        1. Risk Manager preflight (kill_switch, SL/TP, daily loss, etc.)
        2. Получить mark price (для расчёта qty) — через get_tickers
        3. Risk Manager position_size (расчёт qty)
        4. Bybit set_leverage
        5. Bybit place_market_order с атомарными SL/TP
        6. Создать Trade в БД (status="open")
        7. Telegram notify_trade_opened
        8. Вернуть Trade

        Raises:
            RiskCheckFailed: если Risk Manager заблокировал сделку
            ExecutionError: при сбое Bybit или БД
        """
        settings = get_settings()
        side_bybit = _normalize_side(side)

        # 1. Risk Manager preflight (без entry_price — он определится из markPrice)
        # Используем mark price для preflight'а; точный entry будет известен
        # после исполнения market-ордера (но обычно ~равен mark).
        from app.bybit.rest_client import bybit_rest
        if bybit_rest is None:
            raise ExecutionError("bybit_rest not initialized")

        # Получаем mark price для preflight и для position size
        try:
            tickers = await bybit_rest.get_tickers(symbol)
            t = tickers.get("result", {}).get("list", [{}])[0]
            mark_price_str = t.get("markPrice") or t.get("lastPrice") or "0"
            entry_price_approx = Decimal(str(mark_price_str))
        except Exception as e:
            logger.exception("Failed to get mark price for %s", symbol)
            await notify_error(
                action="open_position",
                error=f"mark_price_fetch_failed: {type(e).__name__}",
                symbol=symbol,
            )
            raise ExecutionError(f"mark_price_fetch_failed: {e}") from e

        if entry_price_approx <= 0:
            raise ExecutionError(f"invalid_mark_price: {entry_price_approx}")

        # Preflight — Risk Manager сам проверит kill_switch, SL/TP, daily loss и пр.
        try:
            preflight_result = await risk_manager.preflight(
                symbol=symbol,
                entry_price=entry_price_approx,
                sl_price=sl_price,
                tp_price=tp_price,
                ai_score=ai_score,
                session=session,
            )
        except RiskCheckFailed as e:
            logger.warning("Risk preflight failed for %s: %s", symbol, e)
            await notify_error(
                action="open_position",
                error=f"RiskCheckFailed({e})",
                symbol=symbol,
                details=f"score={ai_score:.1f}",
            )
            raise  # пробрасываем дальше — caller решит что делать

        # 2. Position sizing
        try:
            qty = await risk_manager.position_size(
                symbol=symbol,
                entry_price=entry_price_approx,
                sl_price=sl_price,
                size_factor=size_factor,
            )
        except RiskCheckFailed as e:
            logger.warning("Position sizing failed for %s: %s", symbol, e)
            await notify_error(
                action="open_position",
                error=f"position_size_failed({e})",
                symbol=symbol,
            )
            raise

        # 3. Set leverage (идемпотентно — Bybit возвращает 110043 если уже такое)
        try:
            await self._with_retry(
                bybit_rest.set_leverage,
                symbol=symbol,
                leverage=settings.default_leverage,
            )
        except BybitReadOnlyError as e:
            # readonly_mode выставлен — ничего не делаем
            logger.error("Bybit readonly mode is ON, can't open position")
            await notify_error(
                action="open_position",
                error="readonly_mode_enabled",
                symbol=symbol,
            )
            raise ExecutionError("bybit_readonly_mode") from e
        except Exception as e:
            logger.exception("set_leverage failed for %s", symbol)
            await notify_error(
                action="set_leverage",
                error=f"{type(e).__name__}: {e}",
                symbol=symbol,
            )
            raise ExecutionError(f"set_leverage_failed: {e}") from e

        # 4. Place market order с атомарными SL/TP
        order_link_id = _make_order_link_id(settings.execution_order_link_id_prefix)
        try:
            order_response = await self._with_retry(
                bybit_rest.place_market_order,
                symbol=symbol,
                side=side_bybit,
                qty=qty,
                stop_loss=sl_price,
                take_profit=tp_price,
                order_link_id=order_link_id,
            )
        except BybitReadOnlyError as e:
            await notify_error(
                action="place_market_order",
                error="readonly_mode_enabled",
                symbol=symbol,
            )
            raise ExecutionError("bybit_readonly_mode") from e
        except Exception as e:
            logger.exception("place_market_order failed for %s", symbol)
            await notify_error(
                action="place_market_order",
                error=f"{type(e).__name__}: {e}",
                symbol=symbol,
                details=f"qty={qty} sl={sl_price} tp={tp_price}",
            )
            raise ExecutionError(f"order_failed: {e}") from e

        bybit_order_id = order_response.get("orderId")
        if not bybit_order_id:
            logger.error("Bybit returned no orderId: %s", order_response)
            await notify_error(
                action="place_market_order",
                error="no_order_id_in_response",
                symbol=symbol,
            )
            raise ExecutionError("bybit_no_order_id")

        # 5. Создаём Trade в БД
        repo = TradeRepository(session)
        try:
            risk_pct_decimal = Decimal(str(settings.max_risk_per_trade * 100))
            trade = await repo.create_open_trade(
                symbol=symbol,
                side=side_bybit,
                entry_price=entry_price_approx,
                stop_loss=sl_price,
                take_profit=tp_price,
                position_size=qty,
                leverage=settings.default_leverage,
                bybit_order_id=bybit_order_id,
                signal_id=signal_id,
                timeframe=timeframe,
                risk_percent=risk_pct_decimal,
                final_score=int(ai_score),
                entry_reason=entry_reason,
            )
        except Exception as e:
            # ⚠️ Bybit-сделка уже открыта, а в БД не записалась — критическая ситуация.
            # Логируем громко, уведомляем, но НЕ кидаем — позиция на бирже есть, её
            # нужно потом найти и закрыть руками (через emergency_close по symbol).
            logger.critical(
                "DB write failed AFTER Bybit order placed! "
                "Order is live but not tracked. bybit_order_id=%s symbol=%s qty=%s",
                bybit_order_id, symbol, qty,
            )
            await notify_error(
                action="create_open_trade",
                error=f"DB_INSERT_FAILED_AFTER_BYBIT_ORDER: {type(e).__name__}",
                symbol=symbol,
                details=f"bybit_order_id={bybit_order_id} qty={qty} — manual cleanup needed",
            )
            raise ExecutionError(
                f"db_insert_failed_after_order: bybit_order_id={bybit_order_id}"
            ) from e

        # 6. Telegram уведомление об успехе (best-effort, не падаем если телега лежит)
        await notify_trade_opened(
            symbol=symbol,
            side=side_bybit,
            qty=qty,
            entry_price=entry_price_approx,
            stop_loss=sl_price,
            take_profit=tp_price,
            leverage=settings.default_leverage,
            ai_score=ai_score,
            order_id=str(bybit_order_id),
        )

        logger.info(
            "Position opened: symbol=%s side=%s qty=%s entry=%s SL=%s TP=%s trade_id=%s",
            symbol, side_bybit, qty, entry_price_approx, sl_price, tp_price, trade.id,
        )

        # 7. Метаданные preflight можно использовать caller'у (например webhook handler)
        _ = preflight_result  # noqa: F841 — пока не используется, в Stage 11 пойдёт в snapshot

        return trade

    async def close_position(
        self,
        *,
        trade_id: uuid.UUID,
        session: AsyncSession,
        reason: Optional[str] = None,
    ) -> Trade:
        """
        Закрыть открытую сделку рыночным reduce-only ордером.

        Порядок:
        1. Найти Trade в БД (должна быть status="open")
        2. Bybit place_market_order(reduce_only=True) с противоположным side
        3. Получить mark price для расчёта PnL (приблизительно)
        4. Обновить Trade в БД (status="closed", pnl, result)
        5. Telegram notify_trade_closed

        Raises:
            TradeNotFoundError: если сделка не найдена или уже закрыта
            ExecutionError: при сбое Bybit
        """
        settings = get_settings()
        repo = TradeRepository(session)

        # 1. Найти открытую сделку
        trade = await repo.get_open_trade_by_id(trade_id)
        if trade is None:
            raise TradeNotFoundError(f"Open trade not found: {trade_id}")

        from app.bybit.rest_client import bybit_rest
        if bybit_rest is None:
            raise ExecutionError("bybit_rest not initialized")

        # 2. Противоположный side для закрытия
        close_side = "Sell" if trade.side == "Buy" else "Buy"
        order_link_id = _make_order_link_id(
            settings.execution_order_link_id_prefix + "-close"
        )

        try:
            order_response = await self._with_retry(
                bybit_rest.place_market_order,
                symbol=trade.symbol,
                side=close_side,
                qty=trade.position_size,
                reduce_only=True,
                order_link_id=order_link_id,
            )
        except Exception as e:
            logger.exception("close_position: place_market_order failed")
            await notify_error(
                action="close_position",
                error=f"{type(e).__name__}: {e}",
                symbol=trade.symbol,
                details=f"trade_id={trade.id}",
            )
            raise ExecutionError(f"close_order_failed: {e}") from e

        # 3. Mark price для расчёта exit и PnL (приблизительно)
        try:
            tickers = await bybit_rest.get_tickers(trade.symbol)
            t = tickers.get("result", {}).get("list", [{}])[0]
            exit_price_str = t.get("lastPrice") or t.get("markPrice") or "0"
            exit_price = Decimal(str(exit_price_str))
        except Exception as e:
            # close-ордер уже исполнился, но цены не получили — используем entry как fallback
            logger.warning("Failed to get exit price, using entry as fallback: %s", e)
            exit_price = trade.entry_price

        # 4. Расчёт приблизительного PnL (точный придёт в Stage 11 через get_closed_pnl)
        pnl = _calc_pnl(trade.side, trade.entry_price, exit_price, trade.position_size)

        # 5. Update Trade в БД
        try:
            updated = await repo.close_trade(
                trade_id=trade.id,
                exit_price=exit_price,
                pnl=pnl,
                exit_reason=reason,
            )
        except Exception as e:
            logger.critical(
                "DB update failed AFTER close order placed! trade_id=%s symbol=%s",
                trade.id, trade.symbol,
            )
            await notify_error(
                action="close_trade_db",
                error=f"DB_UPDATE_FAILED_AFTER_CLOSE: {type(e).__name__}",
                symbol=trade.symbol,
                details=f"trade_id={trade.id} — manual reconcile needed",
            )
            raise ExecutionError(f"db_update_failed_after_close: {e}") from e

        if updated is None:
            # race condition: между get_open_trade_by_id и close_trade
            # кто-то закрыл сделку. Маловероятно, но обрабатываем.
            logger.warning("close_trade returned None for trade %s", trade.id)
            return trade

        # 6. Telegram уведомление
        await notify_trade_closed(
            symbol=updated.symbol,
            side=updated.side,
            entry_price=updated.entry_price,
            exit_price=updated.exit_price or exit_price,
            pnl=updated.pnl or pnl,
            result=updated.result or "unknown",
            duration_sec=updated.duration_sec,
            reason=reason,
        )

        # неявный flush, debug
        _ = order_response  # noqa: F841

        logger.info(
            "Position closed: symbol=%s pnl=%s result=%s trade_id=%s",
            updated.symbol, updated.pnl, updated.result, updated.id,
        )
        return updated

    async def emergency_close_all(
        self,
        *,
        session: AsyncSession,
        reason: str = "emergency_close_all",
    ) -> list[Trade]:
        """
        Массовое закрытие всех открытых позиций по всем символам.

        Используется для kill switch (Stage 13: команда /kill в Telegram).

        Возвращает список успешно закрытых Trade. При частичных ошибках
        закрываем что можем, остальное логируем.
        """
        from sqlalchemy import select
        stmt = select(Trade).where(
            Trade.status == "open",
            Trade.closed_at.is_(None),
        )
        result = await session.execute(stmt)
        open_trades = result.scalars().all()

        if not open_trades:
            logger.info("emergency_close_all: no open positions")
            return []

        logger.warning("emergency_close_all: closing %d positions", len(open_trades))
        closed: list[Trade] = []
        for t in open_trades:
            try:
                updated = await self.close_position(
                    trade_id=t.id,
                    session=session,
                    reason=reason,
                )
                closed.append(updated)
            except Exception as e:
                logger.exception(
                    "emergency_close: failed to close trade %s: %s", t.id, e
                )
                await notify_error(
                    action="emergency_close",
                    error=f"{type(e).__name__}: {e}",
                    symbol=t.symbol,
                    details=f"trade_id={t.id}",
                )
                # продолжаем — закрываем что можем

        return closed

    # ========================================================
    # Internal: retry wrapper
    # ========================================================

    async def _with_retry(self, func, *args: Any, **kwargs: Any) -> Any:
        """
        Вызвать func с retry на сетевых/rate-limit ошибках.

        Логические ошибки (BybitAuthError, BybitReadOnlyError, RiskCheckFailed,
        ValueError) НЕ ретраим — они не пройдут от повтора.

        Exponential backoff: settings.execution_retry_delay_sec * 2^n
        """
        settings = get_settings()
        max_retries = settings.execution_max_retries
        base_delay = settings.execution_retry_delay_sec

        last_error: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except (BybitAuthError, BybitReadOnlyError, RiskCheckFailed, ValueError):
                # логические — не ретраим
                raise
            except (BybitNetworkError, BybitRateLimitError) as e:
                last_error = e
                if attempt >= max_retries:
                    logger.error(
                        "Retry exhausted after %d attempts: %s",
                        max_retries, e,
                    )
                    raise
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Retryable error (attempt %d/%d): %s — sleeping %.1fs",
                    attempt + 1, max_retries, e, delay,
                )
                await asyncio.sleep(delay)
            except BybitAPIError as e:
                # API-ошибка с известным кодом — обычно логическая
                logger.error("BybitAPIError (not retrying): %s", e)
                raise

        # сюда не должны дойти, но на всякий случай
        if last_error:
            raise last_error
        raise ExecutionError("retry_logic_error")


# ============================================================
# Singleton
# ============================================================

execution_engine = ExecutionEngine()