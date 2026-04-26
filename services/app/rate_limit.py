import time
from collections import defaultdict

WINDOW_MS = 60_000
MAX_REQ = 120

_buckets: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))


def allow_validate(ip: str) -> bool:
    now = time.time() * 1000
    n, t0 = _buckets[ip]
    if now - t0 > WINDOW_MS:
        _buckets[ip] = (1, now)
        return True
    if n >= MAX_REQ:
        return False
    _buckets[ip] = (n + 1, t0)
    return True
