"""
Auto Trade Worker — слушает AI decisions и плейсит ордеры через AutoTrader.

Работает как фоновый worker:
1. Каждые N секунд проверяет новые AI decisions
2. Если decision = TRADE и score >= threshold → плейсит ордер
3. Логирует все действия
"""

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.database.db import get_sessionmaker
from app.database.models import AiDecision
from app.engines.auto_trader.trader import AutoTrader
from app.config import get_settings

logger = logging.getLogger(__name__)


class AutoTradeWorker:
    """Фоновый worker для автоматического плейсинга ордеров"""
    
    def __init__(self, interval_sec: int = 10):
        self.interval_sec = interval_sec
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_processed_id = 0
        self.auto_trader = AutoTrader()
        self.settings = get_settings()
    
    async def start(self) -> None:
        """Запустить worker"""
        if self._running:
            logger.warning("AutoTradeWorker уже запущен")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"AutoTradeWorker запущен (interval={self.interval_sec}s)")
    
    async def stop(self) -> None:
        """Остановить worker"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AutoTradeWorker остановлен")
    
    async def _run(self) -> None:
        """Основной цикл worker'а"""
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"AutoTradeWorker error: {e}")
            
            await asyncio.sleep(self.interval_sec)
    
    async def _tick(self) -> None:
        """Один тик: проверить новые AI decisions и плейсить ордеры"""
        try:
            sm = get_sessionmaker()
            async with sm() as session:
                # Получаем новые TRADE decisions
                from sqlalchemy import select
                stmt = select(AiDecision).where(
                    (AiDecision.decision == "TRADE") &
                    (AiDecision.id > self._last_processed_id)
                ).order_by(AiDecision.id)
                
                result = await session.execute(stmt)
                decisions = result.scalars().all()
            
            if not decisions:
                return
            
            # Обрабатываем каждое решение
            for decision in decisions:
                logger.info(f"AutoTradeWorker processing decision: {decision.id} {decision.direction}")
                
                # Плейсим ордер через AutoTrader
                trade_result = await self.auto_trader.place_trade_from_ai(decision)
                
                if trade_result["status"] == "ok":
                    logger.info(f"Trade placed from decision {decision.id}: {trade_result}")
                else:
                    logger.warning(f"Trade skipped from decision {decision.id}: {trade_result.get('reason')}")
                
                # Обновляем last_processed_id
                self._last_processed_id = decision.id
        
        except Exception as e:
            logger.exception(f"_tick failed: {e}")


# Singleton
_auto_trade_worker: Optional[AutoTradeWorker] = None


def init_auto_trade_worker(interval_sec: int = 10) -> AutoTradeWorker:
    """Инициализировать AutoTradeWorker"""
    global _auto_trade_worker
    if _auto_trade_worker is not None:
        logger.warning("AutoTradeWorker уже инициализирован")
        return _auto_trade_worker
    
    _auto_trade_worker = AutoTradeWorker(interval_sec=interval_sec)
    return _auto_trade_worker


def get_auto_trade_worker() -> AutoTradeWorker:
    """Получить AutoTradeWorker singleton"""
    if _auto_trade_worker is None:
        raise RuntimeError("AutoTradeWorker не инициализирован")
    return _auto_trade_worker


async def close_auto_trade_worker() -> None:
    """Остановить AutoTradeWorker"""
    global _auto_trade_worker
    if _auto_trade_worker:
        await _auto_trade_worker.stop()
        _auto_trade_worker = None
