"""
Backtest Engine — симулирует исходы AI решений.

Логика:
1. Берёт все TRADE решения из ai_decisions
2. На основе final_score симулирует win/loss
3. Считает метрики (win rate, profit factor, Sharpe, drawdown)
"""

import logging
from typing import Dict, Any, List
from datetime import datetime
import math
import random

from app.database.db import get_sessionmaker
from app.database.models import AiDecision

logger = logging.getLogger(__name__)


class BacktestEngine:
    """Симуляция исходов на основе AI decisions"""
    
    def __init__(self):
        self.trades = []
        self.metrics = {}
    
    def _score_to_win_probability(self, score: float) -> float:
        """На основе score вычисляем вероятность выигрыша"""
        # Логика: score 70 = 50% win, score 85 = 65% win, score 100 = 85% win
        if score < 70:
            return 0.40
        elif score < 75:
            return 0.45
        elif score < 80:
            return 0.50
        elif score < 85:
            return 0.58
        elif score < 90:
            return 0.65
        else:
            return 0.72
    
    def _simulate_trade_outcome(self, score: float, direction: str) -> Dict[str, Any]:
        """Симулирует исход одной сделки на основе score"""
        win_prob = self._score_to_win_probability(score)
        is_win = random.random() < win_prob  # Используем случайность!
        
        # P&L: выигрыш = 2-3% баланса, убыток = -1% баланса
        if is_win:
            pnl = random.uniform(2.0, 3.5)  # 2-3.5% profit
            result = "win"
        else:
            pnl = random.uniform(-1.0, -0.5)  # -1% to -0.5% loss
            result = "loss"
        
        return {
            "result": result,
            "pnl": round(pnl, 2),
            "is_win": is_win,
            "direction": direction,
            "score": score,
        }
    
    async def run_backtest(self, limit: int = 1000) -> Dict[str, Any]:
        """Запустить backtest на всех TRADE решениях"""
        try:
            sm = get_sessionmaker()
            async with sm() as session:
                # Получаем все TRADE решения
                from sqlalchemy import select
                stmt = select(AiDecision).where(
                    AiDecision.decision == "TRADE"
                ).order_by(AiDecision.created_at).limit(limit)
                result = await session.execute(stmt)
                rows = result.scalars().all()
            
            if not rows:
                return {
                    "status": "no_data",
                    "message": "No TRADE decisions found",
                    "total_trades": 0,
                }
            
            # Симулируем каждый trade
            trades = []
            for row in rows:
                outcome = self._simulate_trade_outcome(
                    row.final_score or 70,
                    row.direction or "LONG"
                )
                trades.append(outcome)
            
            # Считаем метрики
            metrics = self._calculate_metrics(trades)
            
            return {
                "status": "ok",
                "total_trades": len(trades),
                "trades": trades,
                "metrics": metrics,
            }
        
        except Exception as e:
            logger.error(f"Backtest failed: {e}")
            return {
                "status": "error",
                "error": str(e),
            }
    
    def _calculate_metrics(self, trades: List[Dict]) -> Dict[str, Any]:
        """Считает метрики по симулированным tradеs"""
        if not trades:
            return {}
        
        wins = sum(1 for t in trades if t["is_win"])
        losses = len(trades) - wins
        win_rate = (wins / len(trades)) * 100 if trades else 0
        
        # Profit factor = total_wins / total_losses
        total_wins = sum(t["pnl"] for t in trades if t["is_win"])
        total_losses = abs(sum(t["pnl"] for t in trades if not t["is_win"]))
        profit_factor = total_wins / total_losses if total_losses > 0 else 0
        
        # Equity curve для drawdown
        equity = [100]  # Starting equity
        for trade in trades:
            new_equity = equity[-1] * (1 + trade["pnl"] / 100)
            equity.append(new_equity)
        
        # Max drawdown
        peak = equity[0]
        max_dd = 0
        for val in equity:
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        
        # Sharpe ratio (упрощённо)
        returns = [equity[i] - equity[i-1] for i in range(1, len(equity))]
        avg_return = sum(returns) / len(returns) if returns else 0
        std_dev = math.sqrt(sum((r - avg_return)**2 for r in returns) / len(returns)) if returns else 1
        sharpe = (avg_return / std_dev) * math.sqrt(252) if std_dev > 0 else 0
        
        final_equity = equity[-1] if equity else 100
        total_return = ((final_equity - 100) / 100) * 100
        
        return {
            "total_trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "total_return": round(total_return, 2),
            "final_equity": round(final_equity, 2),
        }


async def run_backtest(limit: int = 1000) -> Dict[str, Any]:
    """Public функция для запуска backtest"""
    engine = BacktestEngine()
    return await engine.run_backtest(limit=limit)
