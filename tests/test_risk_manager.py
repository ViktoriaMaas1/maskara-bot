"""
Тесты Risk Manager — Stage 3.

Используем monkeypatch (в стиле test_protection.py) для подмены:
- settings.* (kill_switch, bot_paused, лимиты)
- risk_manager._equity (без обращения к Bybit)
- risk_manager._spread_bps (без обращения к Bybit)
- TradeRepository (фейковая реализация без БД)

pytestmark = pytest.mark.unit — все тесты изолированы, не трогают БД/сеть.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.engines.risk_manager import RiskCheckFailed, risk_manager


pytestmark = pytest.mark.unit


# ============================================================
#  FakeTradeRepository — простой mock без БД
# ============================================================
class FakeTradeRepository:
    """Фейковый репозиторий — управляем возвращаемыми значениями из теста."""

    def __init__(
        self,
        *,
        daily_pnl: Decimal = Decimal("0"),
        consecutive_losses: int = 0,
        open_count: int = 0,
    ):
        self._daily_pnl = daily_pnl
        self._consecutive_losses = consecutive_losses
        self._open_count = open_count

    async def get_daily_pnl(self) -> Decimal:
        return self._daily_pnl

    async def get_consecutive_losses(self, limit: int = 10) -> int:
        return self._consecutive_losses

    async def count_open_by_symbol(self, symbol: str) -> int:
        return self._open_count


# ============================================================
#  Helpers
# ============================================================
def _good_args(**overrides):
    """Базовые валидные аргументы для preflight. Тест переопределяет нужные."""
    defaults = dict(
        symbol="BTCUSDT",
        entry_price=Decimal("100000"),
        sl_price=Decimal("99000"),
        tp_price=Decimal("102000"),
        ai_score=85.0,
        session=None,
    )
    defaults.update(overrides)
    return defaults


def _patch_repo(monkeypatch, repo: FakeTradeRepository) -> None:
    """Подменяем TradeRepository, который создаётся внутри preflight()."""
    monkeypatch.setattr(
        "app.engines.risk_manager.TradeRepository",
        lambda session: repo,
    )


def _patch_external(monkeypatch, equity: Decimal = Decimal("1000"),
                    spread_bps: Decimal = Decimal("1")) -> None:
    """Подменяем _equity и _spread_bps (без обращения к Bybit)."""
    async def fake_equity(self):
        return equity

    async def fake_spread(self, symbol):
        return spread_bps

    monkeypatch.setattr(
        "app.engines.risk_manager.RiskManager._equity", fake_equity
    )
    monkeypatch.setattr(
        "app.engines.risk_manager.RiskManager._spread_bps", fake_spread
    )


def _patch_settings(monkeypatch, **overrides):
    """Подмена полей settings."""
    from app.engines.risk_manager import settings
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


# ============================================================
#  Тесты preflight() — правила блокировки сделки
# ============================================================
class TestRiskManagerPreflight:
    """Все проверки pre-trade. Каждая должна блокировать торговлю."""

    async def test_missing_sl_blocks(self, monkeypatch):
        """Без stop-loss => RiskCheckFailed('missing_stop_loss')."""
        _patch_repo(monkeypatch, FakeTradeRepository())
        _patch_external(monkeypatch)

        with pytest.raises(RiskCheckFailed, match="missing_stop_loss"):
            await risk_manager.preflight(**_good_args(sl_price=None))

    async def test_missing_tp_blocks(self, monkeypatch):
        """Без take-profit => RiskCheckFailed('missing_take_profit')."""
        _patch_repo(monkeypatch, FakeTradeRepository())
        _patch_external(monkeypatch)

        with pytest.raises(RiskCheckFailed, match="missing_take_profit"):
            await risk_manager.preflight(**_good_args(tp_price=None))

    async def test_low_score_blocks(self, monkeypatch):
        """AI score < min_final_score_trade => блок."""
        _patch_repo(monkeypatch, FakeTradeRepository())
        _patch_external(monkeypatch)
        _patch_settings(monkeypatch, min_final_score_trade=70)

        with pytest.raises(RiskCheckFailed, match="score_too_low"):
            await risk_manager.preflight(**_good_args(ai_score=50.0))

    async def test_kill_switch_blocks(self, monkeypatch):
        """kill_switch_enabled=True => RiskCheckFailed('kill_switch_enabled')."""
        _patch_settings(monkeypatch, kill_switch_enabled=True)

        with pytest.raises(RiskCheckFailed, match="kill_switch_enabled"):
            await risk_manager.preflight(**_good_args())

    async def test_paused_blocks(self, monkeypatch):
        """bot_paused=True => RiskCheckFailed('bot_paused')."""
        _patch_settings(monkeypatch, kill_switch_enabled=False, bot_paused=True)

        with pytest.raises(RiskCheckFailed, match="bot_paused"):
            await risk_manager.preflight(**_good_args())

    async def test_consecutive_losses_blocks(self, monkeypatch):
        """3 убытка подряд => блок."""
        _patch_repo(monkeypatch, FakeTradeRepository(consecutive_losses=3))
        _patch_external(monkeypatch)
        _patch_settings(monkeypatch,
                        kill_switch_enabled=False,
                        bot_paused=False,
                        min_final_score_trade=70,
                        max_consecutive_losses=3)

        with pytest.raises(RiskCheckFailed, match="consecutive_losses"):
            await risk_manager.preflight(**_good_args())

    async def test_max_open_positions_blocks(self, monkeypatch):
        """Уже есть открытая позиция => блок."""
        _patch_repo(monkeypatch, FakeTradeRepository(open_count=1))
        _patch_external(monkeypatch)