"""
Repository pattern — слой между ORM и бизнес-логикой.

В Stage 11 здесь будет:
    class TradeRepository:
        async def create_open_trade(...)
        async def close_trade(...)
        async def get_open_position(...)
        async def get_daily_pnl(...)
        async def get_last_n_trades(...)
        async def consecutive_losses(...) -> int

Зачем repository а не запросы в endpoint'ах:
- Можно мокать в тестах (вместо реальной БД)
- Бизнес-логика отделена от технологии хранения
- Если когда-то заменим Postgres → удобство замены

Сейчас: пусто — ORM моделей пока некому наполнять.
"""
