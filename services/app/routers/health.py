from fastapi import APIRouter, Request
from redis.asyncio import Redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    db_ok = False
    redis_ok = False
    await db.fetchval("SELECT 1")
    db_ok = True
    await r.ping()
    redis_ok = True
    return {"ok": db_ok and redis_ok, "db": db_ok, "redis": redis_ok}
