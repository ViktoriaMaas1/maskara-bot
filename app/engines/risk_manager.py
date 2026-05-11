"""
Risk Manager — Stage 3.

Hard rules защиты капитала. Проверки идут от самой дешёвой к самой дорогой,
чтобы при первом же нарушении не делать лишних SQL/HTTP запросов.

Обязательные правила:
1.  kill_switch_enabled            (settings, in-memory)
2.  bot_paused                     (settings, in-memory)
3.  stop-loss обязателен           (параметр)
4.  take-profit обязателен         (параметр)
5.  AI score >= min_final_score    (параметр vs settings)
6.  consecutive losses < 3         (SQL через TradeRepository)
7.  daily loss > -3%               (SQL через TradeRepository)
8.  open positions per symbol < 1  (SQL через TradeRepository)
9.  spread < 5 bps                 (HTTP к Bybit через REST)

Если хоть одна проверка падает - кидаем RiskCheckFailed.
Это намеренный fail-closed: лучше пропустить сделку, чем рискнуть капиталом.

TODO (Stage 6 / Telegram bot):
- kill_switch и bot_paused перенести из settings в БД (таблица BotState),
  чтобы переживали рестарт контейнера. Добавить функцию get_bot_state(),
  которая читает БД + кеширует в памяти.

TODO (Stage 11):
- duplicate signals защита уже есть на уровне webhook (app/utils/protection.py),
  здесь её НЕ дублируем.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Final

import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

settings = get_settings()
from app.database.trade_repository import TradeRepository


logger = logging.getLogger(__name__)


# ============================================================
#  Exceptions
# ============================================================
class RiskCheckFailed(Exception):
    """Кидается при любом нарушении risk-правил. Hard fail."""
    pass


# ============================================================
#  Constants
# ============================================================
_BPS_FACTOR: Final[Decimal] = Decimal("10000")  # для перевода в bps


# ============================================================
#  RiskManager
# ============================================================
class RiskManager:
    """Pre-trade проверки и расчёт безопасного размера позиции."""

    async def preflight(
        self,
        *,
        symbol: str,
        entry_price: Decimal,
        sl_price: Decimal | None,
        tp_price: Decimal | None,
        ai_score: float,
        session: AsyncSession,
    ) -> dict:
        """
        Прогоняет все pre-trade проверки.

        Args:
            symbol: торговая пара, например "BTCUSDT"
            entry_price: цена входа в сделку
            sl_price: цена stop-loss (None = нарушение!)
            tp_price: цена take-profit (None = нарушение!)
            ai_score: финальный score от AI Decision Engine (0-100)
            session: активная AsyncSession SQLAlchemy

        Returns:
            dict с ключом "ok": True и метаданными (spread_bps, equity)

        Raises:
            RiskCheckFailed: при первом же нарушении любого правила
        """
        # === Дешёвые проверки (in-memory / параметры) ===
        # 1. Kill switch
        if settings.kill_switch_enabled:
            raise RiskCheckFailed("kill_switch_enabled")

        # 2. Bot paused
        if settings.bot_paused:
            raise RiskCheckFailed("bot_paused")

        # 3. Stop-loss обязателен
        if sl_price is None:
            raise RiskCheckFailed("missing_stop_loss")

        # 4. Take-profit обязателен
        if tp_price is None:
            raise RiskCheckFailed("missing_take_profit")

        # 5. AI score
        if ai_score < settings.min_final_score_trade:
            raise RiskCheckFailed(
                f"score_too_low({ai_score:.1f}<{settings.min_final_score_trade})"
            )

        # === Дорогие проверки (SQL через repository) ===
        repo = TradeRepository(session)

        # 6. Consecutive losses
        cons_losses = await repo.get_consecutive_losses(
            limit=settings.max_consecutive_losses + 1
        )
        if cons_losses >= settings.max_consecutive_losses:
            raise RiskCheckFailed(
                f"consecutive_losses({cons_losses}>={settings.max_consecutive_losses})"
            )

        # 7. Daily loss
        daily_pnl = await repo.get_daily_pnl()
        equity = await self._equity()
        if equity > 0:
            daily_pct = daily_pnl / equity  # обе Decimal, результат Decimal
            max_loss_pct = Decimal(str(-settings.max_daily_loss))
            if daily_pct <= max_loss_pct:
                raise RiskCheckFailed(
                    f"daily_loss_limit({daily_pct:.4f}<={max_loss_pct})"
                )

        # 8. Max open positions per symbol
        open_count = await repo.count_open_by_symbol(symbol)
        if open_count >= settings.max_open_positions_per_symbol:
            raise RiskCheckFailed(
                f"already_open({open_count}>={settings.max_open_positions_per_symbol})"
            )

        # === Сетевые проверки (HTTP к Bybit) ===
        # 9. Spread
        spread_bps = await self._spread_bps(symbol)
        max_spread = Decimal(str(settings.max_spread_bps))
        if spread_bps > max_spread:
            raise RiskCheckFailed(
                f"spread_too_wide({spread_bps:.1f}bps>{max_spread}bps)"
            )

        logger.info(
            f"Risk preflight OK: symbol={symbol} score={ai_score:.1f} "
            f"spread={spread_bps:.1f}bps equity={equity} daily_pnl={daily_pnl}"
        )
        return {
            "ok": True,
            "spread_bps": spread_bps,
            "equity": equity,
            "daily_pnl": daily_pnl,
            "consecutive_losses": cons_losses,
            "open_count": open_count,
        }

    async def position_size(
        self,
        *,
        symbol: str,
        entry_price: Decimal,
        sl_price: Decimal,
        size_factor: Decimal = Decimal("1.0"),
    ) -> Decimal:
        """
        Расчёт безопасного объёма позиции по риску.

        Формула: qty = (equity * risk_per_trade * size_factor) / abs(entry - sl)

        Args:
            symbol: торговая пара (для округления qty к шагу инструмента)
            entry_price: цена входа
            sl_price: цена stop-loss
            size_factor: множитель размера (1.0 = полный, 0.5 = половина)

        Returns:
            qty в виде Decimal, округлённый к шагу инструмента

        Raises:
            RiskCheckFailed: если equity=0, distance=0, или qty<=0
        """
        equity = await self._equity()
        if equity <= 0:
            raise RiskCheckFailed("zero_equity")

        per_unit_risk = abs(entry_price - sl_price)
        if per_unit_risk <= 0:
            raise RiskCheckFailed("invalid_sl_distance")

        risk_pct = Decimal(str(settings.max_risk_per_trade))
        risk_amount = equity * risk_pct * size_factor
        qty = risk_amount / per_unit_risk

        # Округление к шагу инструмента
        # TODO (Stage 5): загружать qty_step через bybit_rest.get_instrument_info()
        # Пока — простое правило: BTC до 0.001, остальные до 0.01
        if symbol.startswith("BTC"):
            qty = qty.quantize(Decimal("0.001"))
        else:
            qty = qty.quantize(Decimal("0.01"))

        if qty <= 0:
            raise RiskCheckFailed("qty_rounded_to_zero")

        logger.debug(
            f"Position size: symbol={symbol} qty={qty} equity={equity} "
            f"risk_amount={risk_amount} size_factor={size_factor}"
        )
        return qty

    async def _equity(self) -> Decimal:
        """
        Получить текущий баланс USDT с Bybit.

        Returns:
            Decimal с балансом. Если API падает - кидает RiskCheckFailed.
            (fail-closed: лучше не торговать, чем торговать вслепую)
        """
        # Lazy import: bybit_rest может быть None если ключи не настроены (тесты)
        from app.bybit.rest_client import bybit_rest

        if bybit_rest is None:
            raise RiskCheckFailed("bybit_rest_not_initialized")

        try:
            res = await bybit_rest.get_balance("USDT")
        except Exception as e:
            logger.exception(f"equity fetch failed: {e}")
            raise RiskCheckFailed(f"equity_unavailable({type(e).__name__})")

        # Bybit V5 формат:
        # {"result": {"list": [{"coin": [{"coin": "USDT", "equity": "1234.56"}]}]}}
        try:
            lst = res.get("result", {}).get("list", [])
            if not lst:
                raise RiskCheckFailed("equity_empty_list")
            coins = lst[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    equity_str = str(c.get("equity", "0"))
                    return Decimal(equity_str)
            raise RiskCheckFailed("equity_no_usdt")
        except RiskCheckFailed:
            raise
        except Exception as e:
            logger.exception(f"equity parse failed: {e}")
            raise RiskCheckFailed(f"equity_parse_error({type(e).__name__})")

    async def _spread_bps(self, symbol: str) -> Decimal:
        """
        Текущий спред в bps (basis points, 1 bps = 0.01%).

        Returns:
            Decimal со spread в bps. Если API падает - кидает RiskCheckFailed.
        """
        from app.bybit.rest_client import bybit_rest

        if bybit_rest is None:
            raise RiskCheckFailed("bybit_rest_not_initialized")

        try:
            res = await bybit_rest.get_tickers(symbol)
        except Exception as e:
            logger.exception(f"spread fetch failed: {e}")
            raise RiskCheckFailed(f"spread_unavailable({type(e).__name__})")

        try:
            t = res.get("result", {}).get("list", [{}])[0]
            bid = Decimal(str(t.get("bid1Price", "0")))
            ask = Decimal(str(t.get("ask1Price", "0")))
            if bid <= 0 or ask <= 0:
                raise RiskCheckFailed("spread_invalid_prices")
            return (ask - bid) / bid * _BPS_FACTOR
        except RiskCheckFailed:
            raise
        except Exception as e:
            logger.exception(f"spread parse failed: {e}")
            raise RiskCheckFailed(f"spread_parse_error({type(e).__name__})")


# ============================================================
#  Singleton instance
# ============================================================
risk_manager = RiskManager()
