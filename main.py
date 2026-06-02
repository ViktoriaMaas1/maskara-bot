from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.utils.redis_client import init_redis, close_redis
from app.cache.market_cache import init_market_cache, close_market_cache, get_market_cache
from app.engines.liquidity.engine import LiquidityEngine

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    await init_market_cache()
    yield
    await close_market_cache()
    await close_redis()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/liquidity/{symbol}")
async def get_liquidity(symbol: str):
    try:
        cache = get_market_cache()
        engine = LiquidityEngine(cache)
        snapshot = await engine.get_snapshot(symbol)
        return snapshot.dict()
    except Exception as e:
        return {"error": str(e), "symbol": symbol}
