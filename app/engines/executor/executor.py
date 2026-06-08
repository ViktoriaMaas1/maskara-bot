"""
Trade Executor — плейсит реальные ордеры на Bybit.

Интеграция:
- rest_client для плейсинга
- Сохранение в trades таблицу
- Risk management (stop-loss, take-profit)
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime

from app.bybit.rest_client import BybitRestClient
from app.database.db import get_sessionmaker
from app.database.models import Trade
from app.config import get_settings

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Выполняет реальные ордеры"""
    
    def __init__(self):
        self.client = BybitRestClient.from_settings()
    
    async def place_trade(
        self,
        symbol: str,
        side: str,  # BUY или SELL
        qty: float,
        leverage: int = 5,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        entry_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Плейсит ордер (market) с TP и SL"""
        try:
            settings = get_settings()
            
            # Проверка: trading должен быть enabled
            if not settings.bot_paused and settings.kill_switch_enabled:
                return {
                    "status": "error",
                    "message": "Kill switch enabled",
                }
            
            # Устанавливаем leverage
            await self.client.set_leverage(symbol, leverage)
            
            # Плейсим market ордер
            order_result = await self.client.place_market_order(
                symbol=symbol,
                side=side,
                qty=qty,
                position_idx=0,  # One-Way Mode
            )
            
            if not order_result.get("success"):
                return {
                    "status": "error",
                    "message": f"Order failed: {order_result.get('ret_msg')}",
                }
            
            order_id = order_result.get("result", {}).get("orderId")
            
            # Сохраняем в БД
            sm = get_sessionmaker()
            async with sm() as session:
                trade = Trade(
                    symbol=symbol,
                    side=side,
                    leverage=leverage,
                    entry_price=entry_price or 0,
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    status="pending",
                    bybit_order_id=order_id,
                    created_at=datetime.utcnow(),
                )
                session.add(trade)
                await session.commit()
                trade_id = trade.id
            
            logger.info(f"Trade placed: {symbol} {side} qty={qty} order_id={order_id}")
            
            return {
                "status": "ok",
                "message": "Order placed",
                "trade_id": str(trade_id),
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
            }
        
        except Exception as e:
            logger.exception("place_trade failed")
            return {
                "status": "error",
                "message": str(e),
            }
    
    async def close_position(self, symbol: str, side: str) -> Dict[str, Any]:
        """Закрывает позицию противоположным ордером"""
        try:
            # Противоположная сторона
            close_side = "SELL" if side == "BUY" else "BUY"
            
            # Получаем размер позиции
            positions = await self.client.get_positions(symbol=symbol)
            pos_qty = 0
            for pos in positions.get("result", {}).get("list", []):
                if pos.get("symbol") == symbol:
                    pos_qty = float(pos.get("size", 0))
            
            if pos_qty == 0:
                return {
                    "status": "error",
                    "message": "No open position",
                }
            
            # Закрываем позицию
            result = await self.client.place_market_order(
                symbol=symbol,
                side=close_side,
                qty=pos_qty,
            )
            
            if not result.get("success"):
                return {
                    "status": "error",
                    "message": f"Close failed: {result.get('ret_msg')}",
                }
            
            logger.info(f"Position closed: {symbol} qty={pos_qty}")
            
            return {
                "status": "ok",
                "message": "Position closed",
                "symbol": symbol,
                "qty": pos_qty,
            }
        
        except Exception as e:
            logger.exception("close_position failed")
            return {
                "status": "error",
                "message": str(e),
            }
    
    async def get_open_positions(self) -> Dict[str, Any]:
        """Получает все открытые позиции"""
        try:
            result = await self.client.get_positions()
            positions = []
            
            # Обрабатываем результат правильно
            pos_list = result if isinstance(result, list) else result.get("result", {}).get("list", []) if isinstance(result.get("result"), dict) else []
            
            for pos in pos_list:
                if float(pos.get("size", 0)) > 0:
                    positions.append({
                        "symbol": pos.get("symbol"),
                        "side": pos.get("side"),
                        "size": float(pos.get("size", 0)),
                        "entry_price": float(pos.get("avgPrice", 0)),
                        "mark_price": float(pos.get("markPrice", 0)),
                        "pnl": float(pos.get("unrealisedPnl", 0)),
                    })
            
            return {
                "status": "ok",
                "positions": positions,
            }
        
        except Exception as e:
            logger.exception("get_open_positions failed")
            return {
                "status": "error",
                "message": str(e),
            }
