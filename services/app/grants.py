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
    wanted_area = fold_area(area)
    for g in grants:
        a = g.get("area")
        lvl = g.get("level", "")
        if isinstance(a, str) and fold_area(a) == wanted_area:
            grant_perms = expand_level(lvl)
            if req_perms <= grant_perms:
                return True
    return False


def fold_area(area: str) -> str:
    """The form in which /validate compares areas: no surrounding whitespace, lower case.

    Zone identity uses the same fold: two zones whose names fold to the same text are
    the same area for /validate, so they cannot both exist (app.zones).
    """
    return area.strip().lower()


def grant_area(elem: Any) -> str | None:
    """The area of one stored grant element as /validate reads it, or None.

    The element comes from json.loads, so a repeated "area" key counts with its last
    value.  Only an object with a string area can be a grant; anything else is never
    one and is kept as it is by the zone rewrites.
    """
    if not isinstance(elem, dict):
        return None
    area = elem.get("area")
    return area if isinstance(area, str) else None


def area_matches_zone(area: str, zone_name: str, other_zone_names: list[str]) -> bool:
    """True when a grant on *area* belongs to the zone *zone_name*.

    The same spelling always does.  Another spelling with the same fold does when no
    other zone has that fold; while zones that differ only in case still exist (from
    before their names were unique regardless of case), only the same spelling counts.
    """
    if area.strip() == zone_name:
        return True
    key = fold_area(area)
    if key != fold_area(zone_name):
        return False
    return all(fold_area(n) != key for n in other_zone_names)


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


def canonical_grants(grants: list[dict[str, str]], allowed: list[str]) -> list[dict[str, str]]:
    """Check *grants* against the allowed areas and return them as they are stored.

    An area is matched to an allowed name regardless of case (fold_area) and stored
    with that name's exact spelling.  The exact spelling wins when several allowed names
    share the fold (zones that differ only in case, from before names had to be unique
    regardless of case); any other spelling is then ambiguous.  Raises ValueError with
    the message for the caller's 400.
    """
    if not allowed:
        return grants
    names = list(dict.fromkeys(allowed))
    exact = set(names)
    by_fold: dict[str, list[str]] = {}
    for n in names:
        by_fold.setdefault(fold_area(n), []).append(n)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for g in grants:
        area = g["area"]
        if area in exact:
            name = area
        else:
            matches = by_fold.get(fold_area(area), [])
            if not matches:
                raise ValueError(f"Area not allowed: {area}")
            if len(matches) > 1:
                raise ValueError(
                    f"Area matches more than one zone ({', '.join(sorted(matches))}). Use the exact name."
                )
            name = matches[0]
        if name in seen:
            raise ValueError(f"Area listed twice: {name}")
        seen.add(name)
        out.append({**g, "area": name})
    return out
