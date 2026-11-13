"""Per-address request limit shared by /validate and /validate/grants.

A fixed window per key (the client address uvicorn reports), kept in memory in each
server process: a key may make `max_req` requests per `window_ms`, counted from its first
request after the previous window ended.  Restarting the process resets every count.
Exempt addresses are never limited or counted.

The limits come from the environment at import, then from app.rate_limit_settings, which
loads the values saved on the admin Settings page and calls apply().

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

import ipaddress
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import get_settings

log = logging.getLogger(__name__)

# The longest text form of an IP address (IPv6 with an embedded IPv4 part).
MAX_ADDRESS_LEN = 45


@dataclass(frozen=True)
class Limits:
    """The limits one process enforces.  `version` is the saved settings version they
    come from (0: environment and defaults only); apply() never goes back to a lower one."""

    version: int
    max_req: int
    window_ms: int
    exempt: frozenset[str] = frozenset()


def limits_from_environment() -> Limits:
    s = get_settings()
    return Limits(0, s.rate_limit_max_req, s.rate_limit_window_ms)


_limits = limits_from_environment()


def current() -> Limits:
    return _limits


def apply(limits: Limits) -> bool:
    """Use `limits` if they come from a newer settings version than the current ones."""
    global _limits
    if limits.version <= _limits.version:
        return False
    _limits = limits
    return True


def address_key(text: str | None) -> str | None:
    """One text form per IP address, or None for anything that is not a plain address.

    IPv4-mapped IPv6 addresses become their IPv4 form, so `::ffff:192.0.2.1` and
    `192.0.2.1` are the same key.  Scoped IPv6 (`fe80::1%eth0`) and networks are refused.
    """
    if not text or len(text) > MAX_ADDRESS_LEN or "%" in text:
        return None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)

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
    def __init__(self) -> None:
        self.started_ms = time.time() * 1000
        self.since_start = _Tally()
        self.last_ms: float | None = None
        self.pending = _Tally()
        self.pending_first_ms: float | None = None
        # Monotonic, so a clock change cannot hold back or hurry the next summary.
        # None until the first summary, which is written at once.
        self.last_summary_mono_ms: float | None = None


_rejections = _Rejections()


def reset_state(limits: Limits | None = None) -> None:
    """Forget all counts and rejections, and set the limits (tests)."""
    global _rejections, _limits
    _buckets.clear()
    _rejections = _Rejections()
    _limits = limits if limits is not None else limits_from_environment()


def allow_validate(ip: str) -> bool:
    now = time.time() * 1000
    lim = _limits
    if lim.exempt and address_key(ip) in lim.exempt:
        _maybe_log_summary(now)
        return True
    n, t0 = _buckets[ip]
    if now - t0 > lim.window_ms:
        _buckets[ip] = (1, now)
        allowed = True
    elif n >= lim.max_req:
        _record_rejection(ip, now)
        allowed = False
    else:
        _buckets[ip] = (n + 1, t0)
        allowed = True
    # Checked on every call, admitted or not, so the rejections at the end of a burst
    # are reported within about one window once any traffic arrives.
    _maybe_log_summary(now)
    return allowed


def retry_after_seconds(ip: str) -> int:
    """Whole seconds until `ip`'s current window ends, at least 1 (for Retry-After)."""
    n, t0 = _buckets.get(ip, (0, 0.0))
    remaining_ms = t0 + _limits.window_ms - time.time() * 1000
    return max(1, math.ceil(remaining_ms / 1000))


def _record_rejection(ip: str, now: float) -> None:
    r = _rejections
    r.since_start.add(ip)
    r.last_ms = now
    if r.pending_first_ms is None:
        r.pending_first_ms = now
    r.pending.add(ip)


def _maybe_log_summary(now: float) -> None:
    r = _rejections
    if not r.pending.total:
        return
    mono = time.monotonic() * 1000
    lim = _limits
    if r.last_summary_mono_ms is not None and mono - r.last_summary_mono_ms < lim.window_ms:
        return
    pending, first = r.pending, r.pending_first_ms
    r.pending = _Tally()
    r.pending_first_ms = None
    r.last_summary_mono_ms = mono
    tops = ", ".join(f"{k}={c}" for k, c in pending.top(SUMMARY_TOP_KEYS))
    shown = min(len(pending.by_key), SUMMARY_TOP_KEYS)
    rest = pending.total - sum(c for _k, c in pending.top(SUMMARY_TOP_KEYS))
    log.warning(
        "Rate limit: rejected %d requests from %s to %s (limit %d per %d ms per address); "
        "top %d addresses: %s%s",
        pending.total,
        _iso(first),
        _iso(now),
        lim.max_req,
        lim.window_ms,
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
    lim = _limits
    return {
        "limit": lim.max_req,
        "windowMs": lim.window_ms,
        "exemptAddresses": len(lim.exempt),
        "since": _iso(r.started_ms),
        "rejectedTotal": t.total,
        "lastRejectedAt": _iso(r.last_ms),
        "topAddresses": [{"address": k, "rejected": c} for k, c in t.top(STATS_TOP_KEYS)],
        "trackedAddresses": len(t.by_key),
        "maxTrackedAddresses": MAX_TRACKED_KEYS,
        # Rejections from addresses beyond the tracked ones: the ranking above leaves them out.
        "untrackedRejected": t.untracked,
    }
