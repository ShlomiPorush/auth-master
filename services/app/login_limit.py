"""Password checks for admin sign-in and registration, and the sign-in attempt limit.

bcrypt is slow on purpose (about 0.2 s per check at cost 12).  Run on the event loop,
a few concurrent sign-ins would hold up every other request, /validate included, so
each bcrypt call runs in a worker thread through anyio, at most BCRYPT_SLOTS at once.
A request that waits longer than SLOT_WAIT_SECONDS for a free slot answers 503.

An unknown username is checked against DUMMY_HASH, so it takes as long as a wrong
password for a real one and the answer time does not tell which usernames exist.
DUMMY_HASH is made at import, before the server accepts requests, with the cost that
registration uses.  (A hash with another cost, which only a hand-edited database can
hold, still works but takes a different time.)

Sign-in attempts are counted per client address: MAX_ATTEMPTS per WINDOW_SECONDS,
counted from the first attempt of the window.  Every attempt counts, before any bcrypt
work, and a correct password does not reset the count.  The address is the one uvicorn
reports (it applies FORWARDED_ALLOW_IPS); IPv6 addresses count per /64 network, because
one client usually holds a whole /64.  Behind a reverse proxy that uvicorn does not
trust, every client has the proxy's address and shares one count.

The counts live in Redis under COUNTER_PREFIX, each key expiring with its window.  The
counter uses its own Redis client without retries (made in the app lifespan): a retry
after a reply was lost would send the transaction again and count the attempt twice.
Redis errors are not hidden: sign-in then answers 500 rather than skip the limit.
"""
from __future__ import annotations

import math
import secrets
from typing import Any, Callable, TypeVar

import anyio
import anyio.to_thread
import bcrypt
from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.rate_limit import bucket_key

MAX_ATTEMPTS = 10
WINDOW_SECONDS = 15 * 60
BCRYPT_SLOTS = 2
SLOT_WAIT_SECONDS = 10
# The cost registration has always used; DUMMY_HASH must take as long as a stored hash.
BCRYPT_COST = 12
COUNTER_PREFIX = "auth:login:addr:"
# Redis client options for the counter: one attempt per command, never a resend.
COUNTER_CLIENT_OPTIONS: dict[str, Any] = {"retry": Retry(NoBackoff(), 0)}

DUMMY_HASH = bcrypt.hashpw(secrets.token_urlsafe(32).encode("ascii"), bcrypt.gensalt(rounds=BCRYPT_COST))

_bcrypt_slots = anyio.CapacityLimiter(BCRYPT_SLOTS)

T = TypeVar("T")


async def run_bcrypt(func: Callable[..., T], *args: Any) -> T:
    """Run a bcrypt function in a worker thread, at most BCRYPT_SLOTS at once.

    A started call always runs to its end; only the wait for a slot is cut short.
    """
    with anyio.move_on_after(SLOT_WAIT_SECONDS):
        return await anyio.to_thread.run_sync(func, *args, limiter=_bcrypt_slots)
    # Reached only when the deadline cut the wait for a slot short.
    raise HTTPException(
        503,
        "The server is busy checking passwords. Try again in a few seconds.",
        headers={"Retry-After": str(SLOT_WAIT_SECONDS)},
    )


def client_key(request: Request) -> str:
    """The address an attempt counts against, as the /validate limit counts it: IPv4,
    the /64 of IPv6, or one shared key for anything that is not an IP address."""
    return bucket_key(request.client.host if request.client else None)


async def count_attempt(r: Redis, key: str) -> tuple[int, int]:
    """Count one attempt for `key`; return the count and the seconds left in its window."""
    name = COUNTER_PREFIX + key
    async with r.pipeline(transaction=True) as pipe:
        # The key is created with its expiry in the same transaction as the increment.
        pipe.set(name, 0, ex=WINDOW_SECONDS, nx=True)
        pipe.incr(name)
        pipe.ttl(name)
        results = await pipe.execute(raise_on_error=False)
    _set, count, ttl = results
    if not isinstance(count, Exception) and (isinstance(ttl, Exception) or ttl < 0):
        # Counted but without an expiry: a command failed inside the transaction, or the
        # key lost its expiry some other way.  Without one it would never go away.
        await r.expire(name, WINDOW_SECONDS)
        ttl = WINDOW_SECONDS
    for result in results:
        if isinstance(result, Exception):
            raise result
    return count, ttl


async def check_attempt(request: Request) -> None:
    """Count this sign-in attempt; 429 once the address is over the limit."""
    count, ttl = await count_attempt(request.app.state.login_redis, client_key(request))
    if count > MAX_ATTEMPTS:
        minutes = max(1, math.ceil(ttl / 60))
        raise HTTPException(
            429,
            f"Too many sign-in attempts from this address. Try again in {minutes} "
            f"minute{'' if minutes == 1 else 's'}.",
            headers={"Retry-After": str(max(1, ttl))},
        )
