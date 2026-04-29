import time
from collections import defaultdict

from app.config import get_settings

settings = get_settings()
WINDOW_MS = settings.rate_limit_window_ms
MAX_REQ = settings.rate_limit_max_req

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
