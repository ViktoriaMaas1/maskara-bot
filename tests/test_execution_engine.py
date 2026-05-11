"""
Тесты Execution Engine — Stage 4.

Стратегия:
- Мокаем ВСЕ внешние зависимости: bybit_rest, risk_manager, TradeRepository, notifier
- Никаких реальных HTTP/БД вызовов
- Проверяем оркестрацию: правильный порядок шагов, правильная обработка ошибок
- pytestmark = pytest.mark.unit — изолированные unit-тесты
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bybit.exceptions import (
    BybitAuthError,
    BybitNetworkError,
    BybitRateLimitError,
    BybitReadOnlyError,
)
from app.engines.execution_engine import (
    ExecutionEngine,
    ExecutionError,
    TradeNotFoundError,
    _calc_pnl,
    _normalize_side,
    execution_engine,
)
from app.engines.risk_manager import RiskCheckFailed

pytestmark = pytest.mark.unit


# ============================================================
# Хелперы
# ============================================================

def _make_fake_trade(
    trade_id: uuid.UUID = None,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    status: str = "open",
    entry_price: Decimal = Decimal("100000"),
    position_size: Decimal = Decimal("0.005"),
) -> MagicMock:
    """Сделать поддельный объект Trade."""
    t = MagicMock()
    t.id = trade_id or uuid.uuid4()
    t.symbol = symbol
    t.side = side
    t.status = status
    t.entry_price = entry_price
    t.exit_price = None
    t.position_size = position_size
    t.stop_loss = Decimal("99000")
    t.take_profit = Decimal("102000")
    t.leverage = 5
    t.pnl = None
    t.result = None
    t.duration_sec = None
    t.closed_at = None
    return t


def _patch_bybit_rest(monkeypatch, **overrides):
    """
    Подменить bybit_rest singleton.

    Возвращает MagicMock с заранее настроенными async-методами.
    Тест может переопределить любой метод через overrides.
    """
    fake = MagicMock()
    # дефолтные успешные ответы
    fake.get_tickers = AsyncMock(return_value={
        "result": {"list": [{
            "markPrice": "100000",
            "lastPrice": "100000",
            "bid1Price": "99999",
            "ask1Price": "100001",
        }]}
    })
    fake.set_leverage = AsyncMock(return_value={})
    fake.place_market_order = AsyncMock(return_value={
        "orderId": "bybit-order-123",
        "orderLinkId": "maskara-abc",
    })

    for name, value in overrides.items():
        setattr(fake, name, value)

    monkeypatch.setattr("app.bybit.rest_client.bybit_rest", fake)
    monkeypatch.setattr("app.engines.execution_engine.bybit_rest", fake, raising=False)
    return fake


def _patch_risk_manager(monkeypatch, preflight_ok=True, qty=Decimal("0.005")):
    """Подменить risk_manager.preflight и position_size."""
    fake_rm = MagicMock()
    if preflight_ok:
        fake_rm.preflight = AsyncMock(return_value={
            "ok": True,
            "spread_bps": Decimal("1"),
            "equity": Decimal("1000"),
            "daily_pnl": Decimal("0"),
            "consecutive_losses": 0,
            "open_count": 0,
        })
    else:
        fake_rm.preflight = AsyncMock(side_effect=RiskCheckFailed("test_block"))
    fake_rm.position_size = AsyncMock(return_value=qty)

    monkeypatch.setattr("app.engines.execution_engine.risk_manager", fake_rm)
    return fake_rm


def _patch_repository(monkeypatch, trade=None, get_open_returns=None):
    """Подменить TradeRepository (как класс, чтобы при new создавался mock)."""
    repo_instance = MagicMock()
    repo_instance.create_open_trade = AsyncMock(
        return_value=trade or _make_fake_trade()
    )
    repo_instance.get_open_trade_by_id = AsyncMock(
        return_value=get_open_returns
    )
    closed = _make_fake_trade(status="closed")
    closed.exit_price = Decimal("102000")
    closed.pnl = Decimal("10")
    closed.result = "win"
    repo_instance.close_trade = AsyncMock(return_value=closed)

    monkeypatch.setattr(
        "app.engines.execution_engine.TradeRepository",
        lambda session: repo_instance,
    )
    return repo_instance


def _patch_notifier(monkeypatch):
    """Подменить все notify_* функции."""
    funcs = {
        "notify_trade_opened": AsyncMock(return_value=True),
        "notify_trade_closed": AsyncMock(return_value=True),
        "notify_error": AsyncMock(return_value=True),
    }
    for name, mock in funcs.items():
        monkeypatch.setattr(f"app.engines.execution_engine.{name}", mock)
    return funcs


# ============================================================
# _normalize_side
# ============================================================

class TestNormalizeSide:
    @pytest.mark.parametrize("inp,expected", [
        ("BUY", "Buy"),
        ("buy", "Buy"),
        ("Buy", "Buy"),
        ("long", "Buy"),
        ("LONG", "Buy"),
        ("SELL", "Sell"),
        ("sell", "Sell"),
        ("Sell", "Sell"),
        ("short", "Sell"),
        ("SHORT", "Sell"),
    ])
    def test_valid(self, inp, expected):
        assert _normalize_side(inp) == expected

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _normalize_side("FLAT")


# ============================================================
# _calc_pnl
# ============================================================

class TestCalcPnl:
    def test_long_profit(self):
        # купили 0.005 BTC по 100000, продали по 102000 → +10
        pnl = _calc_pnl("Buy", Decimal("100000"), Decimal("102000"), Decimal("0.005"))
        assert pnl == Decimal("10")

    def test_long_loss(self):
        pnl = _calc_pnl("Buy", Decimal("100000"), Decimal("99000"), Decimal("0.005"))
        assert pnl == Decimal("-5")

    def test_short_profit(self):
        # шортили по 100000, закрыли по 98000 → +10
        pnl = _calc_pnl("Sell", Decimal("100000"), Decimal("98000"), Decimal("0.005"))
        assert pnl == Decimal("10")

    def test_short_loss(self):
        pnl = _calc_pnl("Sell", Decimal("100000"), Decimal("101000"), Decimal("0.005"))
        assert pnl == Decimal("-5")

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            _calc_pnl("Hold", Decimal("100"), Decimal("100"), Decimal("1"))


# ============================================================
# open_position
# ============================================================

class TestOpenPosition:
    async def test_happy_path(self, monkeypatch):
        """Все ОК — открытие срабатывает, БД и Telegram вызваны."""
        bybit = _patch_bybit_rest(monkeypatch)
        rm = _patch_risk_manager(monkeypatch)
        repo = _patch_repository(monkeypatch)
        notif = _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        trade = await engine.open_position(
            symbol="BTCUSDT",
            side="BUY",
            sl_price=Decimal("99000"),
            tp_price=Decimal("102000"),
            ai_score=85.0,
            session=MagicMock(),
        )

        # Risk Manager вызван
        rm.preflight.assert_awaited_once()
        rm.position_size.assert_awaited_once()

        # Bybit вызван правильно
        bybit.set_leverage.assert_awaited_once()
        bybit.place_market_order.assert_awaited_once()
        call_kwargs = bybit.place_market_order.call_args.kwargs
        assert call_kwargs["symbol"] == "BTCUSDT"
        assert call_kwargs["side"] == "Buy"          # нормализовалось из "BUY"
        assert call_kwargs["stop_loss"] == Decimal("99000")
        assert call_kwargs["take_profit"] == Decimal("102000")

        # БД создала Trade
        repo.create_open_trade.assert_awaited_once()

        # Telegram уведомил
        notif["notify_trade_opened"].assert_awaited_once()

        assert trade is not None

    async def test_risk_check_blocks(self, monkeypatch):
        """RiskCheckFailed — пробрасывается, Bybit не зовётся."""
        bybit = _patch_bybit_rest(monkeypatch)
        rm = _patch_risk_manager(monkeypatch, preflight_ok=False)
        repo = _patch_repository(monkeypatch)
        notif = _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        with pytest.raises(RiskCheckFailed):
            await engine.open_position(
                symbol="BTCUSDT",
                side="BUY",
                sl_price=Decimal("99000"),
                tp_price=Decimal("102000"),
                ai_score=50.0,
                session=MagicMock(),
            )

        # Bybit НЕ должен вызываться после фейла preflight
        bybit.set_leverage.assert_not_awaited()
        bybit.place_market_order.assert_not_awaited()
        repo.create_open_trade.assert_not_awaited()
        # Но Telegram должен был получить notify_error
        notif["notify_error"].assert_awaited()

    async def test_set_leverage_fails_no_order(self, monkeypatch):
        """set_leverage упал — order не отправляется."""
        bybit = _patch_bybit_rest(
            monkeypatch,
            set_leverage=AsyncMock(side_effect=Exception("bybit down")),
        )
        _patch_risk_manager(monkeypatch)
        repo = _patch_repository(monkeypatch)
        _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        with pytest.raises(ExecutionError):
            await engine.open_position(
                symbol="BTCUSDT",
                side="BUY",
                sl_price=Decimal("99000"),
                tp_price=Decimal("102000"),
                ai_score=85.0,
                session=MagicMock(),
            )

        bybit.place_market_order.assert_not_awaited()
        repo.create_open_trade.assert_not_awaited()

    async def test_readonly_mode_raises(self, monkeypatch):
        """BybitReadOnlyError → ExecutionError, без БД."""
        bybit = _patch_bybit_rest(
            monkeypatch,
            set_leverage=AsyncMock(side_effect=BybitReadOnlyError("readonly")),
        )
        _patch_risk_manager(monkeypatch)
        repo = _patch_repository(monkeypatch)
        _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        with pytest.raises(ExecutionError):
            await engine.open_position(
                symbol="BTCUSDT",
                side="BUY",
                sl_price=Decimal("99000"),
                tp_price=Decimal("102000"),
                ai_score=85.0,
                session=MagicMock(),
            )

        repo.create_open_trade.assert_not_awaited()

    async def test_no_order_id_in_response(self, monkeypatch):
        """Bybit вернул пустой orderId — ExecutionError."""
        _patch_bybit_rest(
            monkeypatch,
            place_market_order=AsyncMock(return_value={}),  # нет orderId
        )
        _patch_risk_manager(monkeypatch)
        repo = _patch_repository(monkeypatch)
        _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        with pytest.raises(ExecutionError):
            await engine.open_position(
                symbol="BTCUSDT",
                side="BUY",
                sl_price=Decimal("99000"),
                tp_price=Decimal("102000"),
                ai_score=85.0,
                session=MagicMock(),
            )

        repo.create_open_trade.assert_not_awaited()

    async def test_db_failure_after_order_critical(self, monkeypatch):
        """
        ⚠️ Критическая ситуация: Bybit ордер прошёл, БД упала.
        Должен быть ExecutionError + alert через notify_error.
        """
        bybit = _patch_bybit_rest(monkeypatch)
        _patch_risk_manager(monkeypatch)
        repo = _patch_repository(monkeypatch)
        repo.create_open_trade = AsyncMock(side_effect=Exception("db lost"))
        notif = _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        with pytest.raises(ExecutionError):
            await engine.open_position(
                symbol="BTCUSDT",
                side="BUY",
                sl_price=Decimal("99000"),
                tp_price=Decimal("102000"),
                ai_score=85.0,
                session=MagicMock(),
            )

        # критичный alert должен уйти
        notif["notify_error"].assert_awaited()
        # успешного уведомления об открытии быть не должно
        notif["notify_trade_opened"].assert_not_awaited()

    async def test_sell_side_normalized(self, monkeypatch):
        """side='SELL' → передаётся в Bybit как 'Sell'."""
        bybit = _patch_bybit_rest(monkeypatch)
        _patch_risk_manager(monkeypatch)
        _patch_repository(monkeypatch)
        _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        await engine.open_position(
            symbol="BTCUSDT",
            side="SELL",
            sl_price=Decimal("101000"),
            tp_price=Decimal("98000"),
            ai_score=85.0,
            session=MagicMock(),
        )
        call_kwargs = bybit.place_market_order.call_args.kwargs
        assert call_kwargs["side"] == "Sell"


# ============================================================
# close_position
# ============================================================

class TestClosePosition:
    async def test_happy_path_long(self, monkeypatch):
        """Закрываем long — Bybit получает Sell + reduce_only."""
        trade = _make_fake_trade(side="Buy", entry_price=Decimal("100000"))
        bybit = _patch_bybit_rest(monkeypatch)
        bybit.get_tickers = AsyncMock(return_value={
            "result": {"list": [{"lastPrice": "102000", "markPrice": "102000"}]}
        })
        repo = _patch_repository(monkeypatch, get_open_returns=trade)
        notif = _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        result = await engine.close_position(
            trade_id=trade.id,
            session=MagicMock(),
            reason="take_profit_hit",
        )

        # Закрывающий ордер — противоположная сторона
        call_kwargs = bybit.place_market_order.call_args.kwargs
        assert call_kwargs["side"] == "Sell"
        assert call_kwargs["reduce_only"] is True

        repo.close_trade.assert_awaited_once()
        notif["notify_trade_closed"].assert_awaited_once()
        assert result is not None

    async def test_trade_not_found(self, monkeypatch):
        """Сделка не найдена → TradeNotFoundError."""
        _patch_bybit_rest(monkeypatch)
        _patch_repository(monkeypatch, get_open_returns=None)
        _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        with pytest.raises(TradeNotFoundError):
            await engine.close_position(
                trade_id=uuid.uuid4(),
                session=MagicMock(),
            )

    async def test_close_side_sell_to_buy(self, monkeypatch):
        """Шорт закрывается покупкой."""
        trade = _make_fake_trade(side="Sell")
        bybit = _patch_bybit_rest(monkeypatch)
        _patch_repository(monkeypatch, get_open_returns=trade)
        _patch_notifier(monkeypatch)

        engine = ExecutionEngine()
        await engine.close_position(trade_id=trade.id, session=MagicMock())

        call_kwargs = bybit.place_market_order.call_args.kwargs
        assert call_kwargs["side"] == "Buy"
        assert call_kwargs["reduce_only"] is True


# ============================================================
# _with_retry
# ============================================================

class TestWithRetry:
    async def test_first_try_success(self, monkeypatch):
        """Успех с первого раза — один вызов."""
        func = AsyncMock(return_value="ok")
        engine = ExecutionEngine()
        result = await engine._with_retry(func)
        assert result == "ok"
        assert func.call_count == 1

    async def test_network_error_retries_then_succeeds(self, monkeypatch):
        """Сетевая ошибка → retry → успех."""
        # отвечаем: ошибка, ошибка, успех
        func = AsyncMock(side_effect=[
            BybitNetworkError("timeout"),
            BybitNetworkError("timeout"),
            "ok",
        ])
        # чтобы не ждать реально — мокаем asyncio.sleep
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        engine = ExecutionEngine()
        result = await engine._with_retry(func)
        assert result == "ok"
        assert func.call_count == 3

    async def test_auth_error_not_retried(self, monkeypatch):
        """BybitAuthError → кидаем СРАЗУ, без retry."""
        func = AsyncMock(side_effect=BybitAuthError("bad key"))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        engine = ExecutionEngine()
        with pytest.raises(BybitAuthError):
            await engine._with_retry(func)
        assert func.call_count == 1  # ни одного retry

    async def test_readonly_error_not_retried(self, monkeypatch):
        """BybitReadOnlyError → кидаем сразу."""
        func = AsyncMock(side_effect=BybitReadOnlyError("readonly"))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        engine = ExecutionEngine()
        with pytest.raises(BybitReadOnlyError):
            await engine._with_retry(func)
        assert func.call_count == 1

    async def test_retries_exhausted(self, monkeypatch):
        """Все retries исчерпаны → пробрасывается последняя ошибка."""
        # настройки: max_retries=3 → всего 4 попытки (0,1,2,3)
        func = AsyncMock(side_effect=BybitRateLimitError("rate"))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        engine = ExecutionEngine()
        with pytest.raises(BybitRateLimitError):
            await engine._with_retry(func)
        # max_retries=3 → 1 + 3 = 4 попытки
        assert func.call_count == 4


# ============================================================
# Singleton
# ============================================================

class TestSingleton:
    def test_singleton_exists(self):
        assert execution_engine is not None
        assert isinstance(execution_engine, ExecutionEngine)