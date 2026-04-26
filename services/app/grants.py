from typing import Any, Literal

GrantLevel = Literal["read", "write", "delete", "all"]

# Individual permission flags
_FLAGS = {"read", "write", "delete"}

# All accepted level values (including legacy and combined)
_LEGACY = {"readwrite"}


def normalize_level(level: str) -> str:
    """Normalize and expand level shortcuts.

    - ``"all"``       → ``"read,write,delete"``
    - ``"readwrite"``  → ``"read,write"``   (backward compat)
    - Already-combined strings like ``"read,delete"`` pass through.
    """
    if level == "all":
        return "read,write,delete"
    if level == "readwrite":
        return "read,write"
    return level


def expand_level(level: str) -> set[str]:
    """Return the set of individual permission flags for *level*."""
    parts = {p.strip() for p in normalize_level(level).split(",")}
    return parts & _FLAGS


def _is_valid_level(level: str) -> bool:
    """Return True if *level* is a known single flag, shortcut, or valid combo."""
    if level in _FLAGS or level in _LEGACY or level == "all":
        return True
    parts = {p.strip() for p in level.split(",")}
    return bool(parts) and parts <= _FLAGS


def grant_covers(grants: list[dict[str, Any]], area: str, required: str) -> bool:
    """Check if any grant for *area* includes every flag in *required*.

    Each permission flag is independent.  A grant with ``"read,delete"``
    covers a request for ``"read"`` or ``"delete"`` but **not** ``"write"``.
    """
    req_perms = expand_level(required)
    if not req_perms:
        return False
    wanted_area = area.strip().lower()
    for g in grants:
        grant_area = g.get("area")
        lvl = g.get("level", "")
        if isinstance(grant_area, str) and grant_area.strip().lower() == wanted_area:
            grant_perms = expand_level(lvl)
            if req_perms <= grant_perms:
                return True
    return False


def parse_grants(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        area = item.get("area")
        level = item.get("level")
        if not isinstance(area, str) or not area.strip():
            continue
        if not isinstance(level, str) or not _is_valid_level(level):
            continue
        a = area.strip()
        if a in seen:
            continue
        seen.add(a)
        out.append({"area": a, "level": normalize_level(level)})
    return out


def assert_grants_allowed(grants: list[dict[str, str]], allowed: list[str]) -> None:
    if not allowed:
        return
    for g in grants:
        if g["area"] not in allowed:
            raise ValueError(f"Area not allowed: {g['area']}")
