"""
Real-Time Market Cache — Stage 6.

Хранилище рыночных данных в Redis (orderbook, trades, tickers, klines, liquidations).
Используется как источник истины для Order Flow Engine, Liquidity Engine,
Risk Manager и AI Decision Engine.

Данные пишутся из app/bybit/websocket_client.py callbacks,
читаются всеми компонентами через get_market_cache().
"""
