"""
AutoTrader — автоматический плейсинг ордеров на основе AI decisions.

Логика:
1. Слушает каждое TRADE решение от AI
2. Проверяет conditions (score, confidence, risk)
3. Рассчитывает position size
4. Плейсит ордер
5. Отслеживает P&L
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from app.config import get_settings
from app.database.db import get_sessionmaker
from app.database.models import Trade, AiDecision
from app.engines.executor.executor import TradeExecutor
from app.api.settings import get_bot_state, get_balance

logger = logging.getLogger(__name__)


class AutoTrader:
    """Автоматическая торговля на основе AI decisions"""
    
    def __init__(self):
        self.executor = TradeExecutor()
        self.settings = get_settings()
    
    async def should_trade(self, decision: AiDecision) -> tuple[bool, str]:
        """Проверяет все условия для плейсинга"""
        
        # 1. Decision type
        if decision.decision != "TRADE":
            return False, "Not a TRADE decision"
        
        # 2. Score threshold
        if decision.final_score < self.settings.min_final_score_trade:
            return False, f"Score {decision.final_score} < {self.settings.min_final_score_trade}"
        
        # 3. Confidence
        if decision.confidence not in ["MEDIUM", "HIGH"]:
            return False, f"Confidence {decision.confidence} too low"
        
        # 4. Bot state
        state = await get_bot_state()
        if not state.get("trading_enabled"):
            return False, "Trading disabled"
        
        if state.get("bot_paused"):
            return False, "Bot paused"
        
        # 5. Kill switch
        if self.settings.kill_switch_enabled:
            return False, "Kill switch enabled"
        
        # 6. Daily loss limit
        daily_loss = await self._get_daily_loss()
        if daily_loss < -self.settings.max_daily_loss * 100:  # Convert to units
            return False, f"Daily loss limit exceeded: {daily_loss}%"
        
        # 7. Max consecutive losses
        consecutive_losses = await self._get_consecutive_losses()
        if consecutive_losses >= self.settings.max_consecutive_losses:
            return False, f"Max consecutive losses: {consecutive_losses}"
        
        return True, "All checks passed"
    
    async def calculate_position_size(
        self,
        symbol: str,
        balance: float,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Рассчитывает размер позиции на основе risk management"""
        
        # Risk per trade в процентах
        risk_pct = self.settings.max_risk_per_trade  # 0.01 = 1%
        
        # Расстояние до stop loss
        if entry_price == 0 or stop_loss == 0:
            return 0.1  # Default qty
        
        sl_distance = abs(entry_price - stop_loss) / entry_price
        if sl_distance == 0:
            return 0.1
        
        # Размер позиции = (balance * risk_pct) / sl_distance
        position_value = balance * risk_pct / sl_distance
        qty = position_value / entry_price
        
        # Минимум 0.01, максимум 100
        return max(0.01, min(qty, 100))
    
    async def place_trade_from_ai(self, decision: AiDecision) -> Dict[str, Any]:
        """Плейсит ордер на основе AI decision"""
        
        # Проверяем conditions
        can_trade, reason = await self.should_trade(decision)
        if not can_trade:
            logger.info(f"AutoTrader skipped: {reason}")
            return {
                "status": "skipped",
                "reason": reason,
            }
        
        try:
            # Получаем balance
            balance = await get_balance()
            if not balance:
                return {
                    "status": "error",
                    "message": "Balance unavailable",
                }
            
            # Получаем текущую цену (из decision context)
            entry_price = decision.full_response.get("entry_price", 0) if decision.full_response else 0
            stop_loss = decision.full_response.get("stop_loss", 0) if decision.full_response else 0
            take_profit = decision.full_response.get("take_profit", 0) if decision.full_response else 0
            
            # Рассчитываем размер позиции
            qty = await self.calculate_position_size(
                decision.full_response.get("symbol", "BTCUSDT"),
                balance,
                entry_price,
                stop_loss,
            )
            
            # Плейсим ордер
            result = await self.executor.place_trade(
                symbol=decision.full_response.get("symbol", "BTCUSDT"),
                side=decision.direction or "LONG",
                qty=qty,
                leverage=self.settings.default_leverage,
                take_profit=take_profit if take_profit > 0 else None,
                stop_loss=stop_loss if stop_loss > 0 else None,
                entry_price=entry_price if entry_price > 0 else None,
            )
            
            if result["status"] == "ok":
                logger.info(f"AutoTrader placed trade: {result}")
                # Сохраняем связь между AI decision и trade
                sm = get_sessionmaker()
                async with sm() as session:
                    trade = await session.query(Trade).filter(
                        Trade.id == int(result["trade_id"])
                    ).first()
                    if trade:
                        trade.ai_decision_id = decision.id
                        await session.commit()
            
            return result
        
        except Exception as e:
            logger.exception("AutoTrader failed")
            return {
                "status": "error",
                "message": str(e),
            }
    
    async def _get_daily_loss(self) -> float:
        """Получает дневной убыток в %"""
        sm = get_sessionmaker()
        async with sm() as session:
            today_trades = await session.query(Trade).filter(
                Trade.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0)
            ).all()
            
            total_pnl = sum(float(t.pnl or 0) for t in today_trades)
            return (total_pnl / 100) if total_pnl != 0 else 0
    
    async def _get_consecutive_losses(self) -> int:
        """Получает количество подряд идущих убытков"""
        sm = get_sessionmaker()
        async with sm() as session:
            closed_trades = await session.query(Trade).filter(
                Trade.status == "closed"
            ).order_by(Trade.closed_at.desc()).limit(20).all()
            
            consecutive = 0
            for trade in closed_trades:
                if trade.result == "loss":
                    consecutive += 1
                else:
                    break
            
            return consecutive
