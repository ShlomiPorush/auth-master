import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

router = APIRouter(tags=["health"])

# Seconds each dependency may take to answer. Both checks run concurrently, so
# the endpoint stays well inside the Docker HEALTHCHECK timeout (5s).
CHECK_TIMEOUT_SEC = 2.0


async def _check(name: str, probe) -> bool:
    try:
        await asyncio.wait_for(probe(), timeout=CHECK_TIMEOUT_SEC)
        return True
    except Exception as e:
        # Only the exception class: messages can carry connection details.
        print(f"Health check: {name} is not responding ({type(e).__name__})")
        return False


@router.get("/health")
async def health(request: Request):
    db = request.app.state.db
    r: Redis = request.app.state.redis
    db_ok, redis_ok = await asyncio.gather(
        _check("database", lambda: db.fetchval("SELECT 1")),
        _check("redis", r.ping),
    )
    ok = db_ok and redis_ok
    # 503 on an outage, so the Docker HEALTHCHECK (urllib raises on it) fails.
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "db": db_ok, "redis": redis_ok},
    )
