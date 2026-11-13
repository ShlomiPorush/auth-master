"""Rate limit settings saved on the admin Settings page.

Stored in the table rate_limit_settings, one row per saved version: a save inserts the
next version and nothing is ever updated or deleted, so the table is also the history of
who changed what, when, and from which address.  The current settings are the row with
the highest version; an empty table is version 0.

Effective value of each limit: the environment variable when it is set (the field is
then locked on the page), else the stored value, else the default.  Exempt addresses
exist only here.

Settings are read and written on the database handle from db.open_settings_database(),
where every statement commits on its own.  Each process loads them at startup and again
every RELOAD_INTERVAL_SEC seconds; app.rate_limit.apply() only moves to newer versions.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app import rate_limit
from app.config import Settings, get_settings
from app.db import Database, UniqueViolationError

FORMAT = 1
DEFAULT_MAX_REQ: int = Settings.model_fields["rate_limit_max_req"].default
DEFAULT_WINDOW_MS: int = Settings.model_fields["rate_limit_window_ms"].default
MAX_REQ_LIMIT = 1_000_000
WINDOW_SECONDS_LIMIT = 3600
MAX_EXEMPT = 100
RELOAD_INTERVAL_SEC = 15

# Field name on the page and in the API -> Settings attribute and environment variable.
ENV_FIELDS = {
    "maxReq": ("rate_limit_max_req", "RATE_LIMIT_MAX_REQ"),
    "windowMs": ("rate_limit_window_ms", "RATE_LIMIT_WINDOW_MS"),
}


class InvalidSettings(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class Stored:
    version: int
    max_req: int | None
    window_ms: int | None
    exempt: tuple[str, ...]
    actor: str | None = None
    ip_address: str | None = None
    created_at: Any = None


EMPTY = Stored(0, None, None, ())


# ── Validation (saving and loading use the same checks) ─────────

def check_max_req(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REQ_LIMIT:
        raise InvalidSettings("maxReq", f"Requests per window must be a whole number from 1 to {MAX_REQ_LIMIT:,}.")
    return value


def check_window_ms(value: Any) -> int:
    if type(value) is not int or value % 1000 or not 1 <= value // 1000 <= WINDOW_SECONDS_LIMIT:
        raise InvalidSettings("windowMs", f"The window must be a whole number of seconds from 1 to {WINDOW_SECONDS_LIMIT:,}.")
    return value


def check_exempt(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise InvalidSettings("exempt", "Exempt addresses must be a list.")
    keys: set[str] = set()
    for text in values:
        if not isinstance(text, str):
            raise InvalidSettings("exempt", "Each exempt address must be text.")
        entry = text.strip()
        if not entry:
            continue
        key = rate_limit.address_key(entry)
        if key is None:
            shown = entry if len(entry) <= rate_limit.MAX_ADDRESS_LEN else entry[: rate_limit.MAX_ADDRESS_LEN] + "..."
            # Text that is not valid UTF-8 (a lone surrogate from a JSON escape) is shown escaped.
            shown = shown.encode("utf-8", "backslashreplace").decode("utf-8")
            raise InvalidSettings(
                "exempt",
                f'"{shown}" is not a single IPv4 or IPv6 address. Write one address per line, '
                "without a network size (/24) or an IPv6 zone (%eth0).",
            )
        keys.add(key)
    if len(keys) > MAX_EXEMPT:
        raise InvalidSettings("exempt", f"At most {MAX_EXEMPT} exempt addresses are allowed.")
    return tuple(sorted(keys))


def parse_value(text: str) -> tuple[int | None, int | None, tuple[str, ...]]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        raise InvalidSettings("value", "The stored value is not valid JSON.") from e
    if (
        not isinstance(data, dict)
        or type(data.get("format")) is not int
        or data["format"] != FORMAT
        or set(data) != {"format", "maxReq", "windowMs", "exempt"}
    ):
        raise InvalidSettings("value", f"The stored value is not in format {FORMAT}.")
    max_req = None if data["maxReq"] is None else check_max_req(data["maxReq"])
    window_ms = None if data["windowMs"] is None else check_window_ms(data["windowMs"])
    exempt = check_exempt(data["exempt"])
    if list(exempt) != data["exempt"]:
        raise InvalidSettings("exempt", "The stored exempt addresses are not in their normal form.")
    return max_req, window_ms, exempt


def dump_value(max_req: int | None, window_ms: int | None, exempt: tuple[str, ...]) -> str:
    return json.dumps({"format": FORMAT, "maxReq": max_req, "windowMs": window_ms, "exempt": list(exempt)}, sort_keys=True)


# ── Effective values ────────────────────────────────────────────

def locked_fields() -> dict[str, bool]:
    """Fields set by the environment (or .env): pydantic records which fields its sources set."""
    explicitly_set = get_settings().model_fields_set
    return {field: attr in explicitly_set for field, (attr, _env) in ENV_FIELDS.items()}


def effective(stored: Stored) -> rate_limit.Limits:
    s = get_settings()
    locked = locked_fields()
    if locked["maxReq"]:
        max_req = s.rate_limit_max_req
    else:
        max_req = stored.max_req if stored.max_req is not None else DEFAULT_MAX_REQ
    if locked["windowMs"]:
        window_ms = s.rate_limit_window_ms
    else:
        window_ms = stored.window_ms if stored.window_ms is not None else DEFAULT_WINDOW_MS
    return rate_limit.Limits(stored.version, max_req, window_ms, frozenset(stored.exempt))


# ── Database ────────────────────────────────────────────────────

_SELECT_CURRENT = (
    "SELECT version, value, actor, ip_address, created_at FROM rate_limit_settings "
    "ORDER BY version DESC LIMIT 1"
)


async def read_current(db: Database) -> Stored:
    row = await db.fetchrow(_SELECT_CURRENT)
    if row is None:
        return EMPTY
    try:
        max_req, window_ms, exempt = parse_value(row["value"])
    except InvalidSettings as e:
        raise InvalidSettings(
            e.field,
            f"Rate limit settings version {row['version']} in the table rate_limit_settings are invalid: "
            f"{e.message} See \"Repairing stored rate limit settings\" in README.md.",
        ) from e
    return Stored(int(row["version"]), max_req, window_ms, exempt, row["actor"], row["ip_address"], row["created_at"])


async def insert_version(db: Database, version: int, value: str, actor: str, ip_address: str | None) -> bool:
    """Save `value` as `version`.  False when that version already exists (someone saved first)."""
    try:
        await db.execute(
            "INSERT INTO rate_limit_settings (version, value, actor, ip_address) VALUES ($1, $2, $3, $4)",
            version, value, actor, ip_address,
        )
    except UniqueViolationError:
        return False
    return True


# ── Loading and reloading ───────────────────────────────────────

@dataclass
class ReloadStatus:
    last_ok_ms: float | None = None
    last_error: str | None = None


status = ReloadStatus()


def _iso(ms: float | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def status_view() -> dict[str, Any]:
    return {
        "version": rate_limit.current().version,
        "lastReload": _iso(status.last_ok_ms),
        "lastReloadError": status.last_error,
    }


async def load(db: Database) -> None:
    """At startup.  Invalid stored settings stop the service: the intended limits are unknown."""
    try:
        stored = await read_current(db)
    except InvalidSettings as e:
        raise RuntimeError(e.message) from e
    rate_limit.apply(effective(stored))
    status.last_ok_ms = time.time() * 1000
    status.last_error = None


async def reload_once(db: Database) -> None:
    """Apply newer saved settings; on any failure keep the current ones and record why."""
    try:
        stored = await read_current(db)
    except InvalidSettings as e:
        status.last_error = e.message
        return
    except Exception as e:  # database unreachable or locked: keep the last good settings
        # Only the class name: driver messages can carry connection details.
        status.last_error = f"Could not read the settings ({type(e).__name__})."
        return
    rate_limit.apply(effective(stored))
    status.last_ok_ms = time.time() * 1000
    status.last_error = None


async def reload_loop(db: Database) -> None:
    while True:
        await asyncio.sleep(RELOAD_INTERVAL_SEC)
        await reload_once(db)
