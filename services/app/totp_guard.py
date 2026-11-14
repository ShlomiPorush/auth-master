"""TOTP checks with the state that pyotp leaves to the application.

pyotp checks a code; it does not limit attempts or remember used codes. This module
adds both, in Redis:

- an attempt counter per admin with a fixed window, reserved before the code is
  checked, so parallel guesses cannot pass the limit;
- a marker per admin and time step, so an accepted code is not accepted again
  (RFC 6238 section 5.2).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import pyotp
from fastapi import HTTPException, Request
from redis.asyncio import Redis

from app.logger import log_activity

MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 15 * 60
# Longer than the ~90 s a code stays valid with valid_window=1.
USED_STEP_TTL = 180

FAIL_PREFIX = "auth:mfa-fail:"
USED_PREFIX = "auth:totp-used:"

REUSED_TEXT = "This code was already used. Wait for the next code in your authenticator app."

# The clock; tests replace it.
_now = time.time

Reason = Literal["invalid", "reused", "locked"]


@dataclass(frozen=True)
class TotpCheck:
    ok: bool
    reason: Reason | None = None
    # This failed attempt used the last one of the window.
    locked_now: bool = False
    retry_after: int = 0


def _step(totp: pyotp.TOTP, t: float) -> int:
    return totp.timecode(datetime.fromtimestamp(t, tz=timezone.utc))


def _matched_step(totp: pyotp.TOTP, code: str, now: float) -> int | None:
    # Same acceptance as verify(code, valid_window=1), but tells which step matched.
    for offset in (0, -1, 1):
        t = now + offset * totp.interval
        if totp.verify(code, for_time=int(t), valid_window=0):
            return _step(totp, t)
    return None


async def _reserve_attempt(r: Redis, admin_id: str) -> tuple[int, int]:
    key = FAIL_PREFIX + admin_id
    async with r.pipeline(transaction=True) as pipe:
        pipe.set(key, 0, nx=True, ex=ATTEMPT_WINDOW)
        pipe.incr(key)
        pipe.ttl(key)
        _, count, ttl = await pipe.execute()
    if ttl < 0:
        # The key expired between SET and INCR (Redis before 6 lets time pass inside
        # EXEC) and INCR created it without a TTL; a lockout must still end.
        await r.expire(key, ATTEMPT_WINDOW)
        ttl = ATTEMPT_WINDOW
    return int(count), int(ttl)


async def _claim_code(r: Redis, admin_id: str, secret: str, code: str) -> Reason | None:
    totp = pyotp.TOTP(secret)
    step = _matched_step(totp, code, _now())
    if step is None:
        return "invalid"
    claimed = await r.set(f"{USED_PREFIX}{admin_id}:{step}", 1, nx=True, ex=USED_STEP_TTL)
    if not claimed:
        return "reused"
    # A request that stalled after the check may claim a step whose marker already
    # expired; the step must still be valid now. The marker stays either way.
    current = _step(totp, _now())
    if not current - 1 <= step <= current + 1:
        return "invalid"
    return None


async def check_totp_code(
    r: Redis, admin_id: str, secret: str, code: str, *, count_attempt: bool = True
) -> TotpCheck:
    """Check a code for an admin. Redis errors propagate: never read as success."""
    admin_id = str(admin_id)
    count = ttl = 0
    if count_attempt:
        count, ttl = await _reserve_attempt(r, admin_id)
        if count > MAX_ATTEMPTS:
            return TotpCheck(ok=False, reason="locked", retry_after=max(ttl, 1))
    reason = await _claim_code(r, admin_id, secret, code)
    if reason is None:
        if count_attempt:
            await r.delete(FAIL_PREFIX + admin_id)
        return TotpCheck(ok=True)
    locked_now = count_attempt and count >= MAX_ATTEMPTS
    return TotpCheck(ok=False, reason=reason, locked_now=locked_now, retry_after=max(ttl, 1) if locked_now else 0)


def _minutes(seconds: int) -> str:
    n = max(1, math.ceil(seconds / 60))
    return f"{n} minute" if n == 1 else f"{n} minutes"


def totp_error(check: TotpCheck, *, at_login: bool) -> HTTPException:
    """The answer for a failed check: 429 once the attempts are used up, else 400."""
    if check.reason == "locked" or check.locked_now:
        then = "Sign in again" if at_login else "Try again"
        return HTTPException(
            429,
            f"Too many incorrect codes. {then} in {_minutes(check.retry_after)}.",
            headers={"Retry-After": str(check.retry_after)},
        )
    if check.reason == "reused":
        return HTTPException(400, REUSED_TEXT)
    return HTTPException(400, "Invalid code")


async def log_totp_failure(request: Request, admin_id: str, where: str, check: TotpCheck) -> None:
    """Activity rows for a failed code. Attempts refused while locked write none."""
    if check.reason == "locked":
        return
    db = request.app.state.db
    admin_id = str(admin_id)
    username = await db.fetchval("SELECT username FROM admin_users WHERE id = $1", admin_id)
    common = {
        "actor": username or f"admin:{admin_id}",
        "entity_type": "admin_user",
        "entity_id": admin_id,
        "entity_name": username,
        "ip_address": request.client.host if request.client else "unknown",
    }
    await log_activity(db, action="mfa_failed", details={"where": where, "reason": check.reason}, **common)
    if check.locked_now:
        await log_activity(
            db, action="mfa_locked", details={"where": where, "seconds": check.retry_after}, **common
        )
