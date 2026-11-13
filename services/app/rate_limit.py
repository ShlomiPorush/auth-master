"""Per-address request limit shared by /validate and /validate/grants.

A fixed window per key (the client address uvicorn reports), kept in memory in each
server process: a key may make MAX_REQ requests per WINDOW_MS, counted from its first
request after the previous window ended.  Restarting the process resets every count.

Rejected requests are not written to the access log, so this module also keeps a
bounded record of them for operators:

- a summary line at WARNING level, at most once per window per process, with the
  number of rejections since the previous line and the addresses that caused most;
- running totals since the process started, read by the admin API.

Neither touches the database, so a rejection never waits for it or fails with it.
Every function here is synchronous: requests in one process cannot interleave
inside them.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from app.config import get_settings

log = logging.getLogger(__name__)

settings = get_settings()
WINDOW_MS = settings.rate_limit_window_ms
MAX_REQ = settings.rate_limit_max_req

# Addresses counted by name, separately for the totals and for the pending summary.
# Rejections from further addresses are counted without a name, so memory stays
# bounded whatever number of addresses a client uses.
MAX_TRACKED_KEYS = 1000
SUMMARY_TOP_KEYS = 10
STATS_TOP_KEYS = 20

_buckets: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))


class _Tally:
    """Rejections per address, bounded to MAX_TRACKED_KEYS named addresses."""

    def __init__(self) -> None:
        self.total = 0
        self.by_key: dict[str, int] = {}
        self.untracked = 0

    def add(self, key: str) -> None:
        self.total += 1
        if key in self.by_key:
            self.by_key[key] += 1
        elif len(self.by_key) < MAX_TRACKED_KEYS:
            self.by_key[key] = 1
        else:
            self.untracked += 1

    def top(self, n: int) -> list[tuple[str, int]]:
        return sorted(self.by_key.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


class _Rejections:
    def __init__(self, now_ms: float) -> None:
        self.started_ms = now_ms
        self.since_start = _Tally()
        self.last_ms: float | None = None
        self.pending = _Tally()
        self.pending_first_ms: float | None = None
        # 0 so that the first rejection after a quiet period is reported at once.
        self.last_summary_ms = 0.0


_rejections = _Rejections(time.time() * 1000)


def reset_state(now_ms: float | None = None) -> None:
    """Forget all counts and rejections (tests)."""
    global _rejections
    _buckets.clear()
    _rejections = _Rejections(time.time() * 1000 if now_ms is None else now_ms)


def allow_validate(ip: str, now_ms: float | None = None) -> bool:
    now = time.time() * 1000 if now_ms is None else now_ms
    n, t0 = _buckets[ip]
    if now - t0 > WINDOW_MS:
        _buckets[ip] = (1, now)
        allowed = True
    elif n >= MAX_REQ:
        _record_rejection(ip, now)
        allowed = False
    else:
        _buckets[ip] = (n + 1, t0)
        allowed = True
    # Checked on every call, admitted or not, so the rejections at the end of a burst
    # are reported within about one window once any traffic arrives.
    _maybe_log_summary(now)
    return allowed


def _record_rejection(ip: str, now: float) -> None:
    r = _rejections
    r.since_start.add(ip)
    r.last_ms = now
    if r.pending_first_ms is None:
        r.pending_first_ms = now
    r.pending.add(ip)


def _maybe_log_summary(now: float) -> None:
    r = _rejections
    if not r.pending.total or now - r.last_summary_ms < WINDOW_MS:
        return
    pending, first = r.pending, r.pending_first_ms
    r.pending = _Tally()
    r.pending_first_ms = None
    r.last_summary_ms = now
    tops = ", ".join(f"{k}={c}" for k, c in pending.top(SUMMARY_TOP_KEYS))
    shown = min(len(pending.by_key), SUMMARY_TOP_KEYS)
    rest = pending.total - sum(c for _k, c in pending.top(SUMMARY_TOP_KEYS))
    log.warning(
        "Rate limit: rejected %d requests from %s to %s (limit %d per %d ms per address); "
        "top %d addresses: %s%s",
        pending.total,
        _iso(first),
        _iso(now),
        MAX_REQ,
        WINDOW_MS,
        shown,
        tops or "none",
        f"; {rest} more from other addresses" if rest else "",
    )


def _iso(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def rejection_stats() -> dict:
    """Totals since this process started, for the admin API.  Read-only."""
    r = _rejections
    t = r.since_start
    return {
        "limit": MAX_REQ,
        "windowMs": WINDOW_MS,
        "since": _iso(r.started_ms),
        "rejectedTotal": t.total,
        "lastRejectedAt": _iso(r.last_ms),
        "topAddresses": [{"address": k, "rejected": c} for k, c in t.top(STATS_TOP_KEYS)],
        "trackedAddresses": len(t.by_key),
        "maxTrackedAddresses": MAX_TRACKED_KEYS,
        # Rejections from addresses beyond the tracked ones: the ranking above leaves them out.
        "untrackedRejected": t.untracked,
    }
