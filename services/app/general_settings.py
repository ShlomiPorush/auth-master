"""General settings saved on the admin Settings page: the time zone of every date the
service shows, the Secure flag of the admin session cookie, and how many days the
activity log and the access log are kept.

Stored like the rate limit settings (app.rate_limit_settings), in the append-only table
app_settings: one row per saved version, never updated or deleted.  An empty table is
version 0, and a field that was never saved (null) uses its default.

Effective value of each field: the environment variable when it is set (the field is then
locked on the page), else the stored value, else the default.  An environment TZ that is
empty or not a known zone locks the field and dates stay in UTC.

While a variable is set, the stored value follows it: at startup, every field the
environment sets with a valid value that differs from the stored one is saved as a new
version (actor "environment").  Removing the variable later keeps the last value.

Each process holds the effective values in memory (current()), loads them at startup,
reloads them every RELOAD_INTERVAL_SEC seconds, and applies its own saves at once.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from app.config import Settings, get_settings
from app.db import Database, UniqueViolationError

FORMAT = 1
RETENTION_MIN = 1
RETENTION_MAX = 3650
SYNC_ATTEMPTS = 5
ENVIRONMENT_ACTOR = "environment"
RELOAD_INTERVAL_SEC = 15  # the reload loop in app.main

# Field name on the page and in the API -> Settings attribute and environment variable.
ENV_FIELDS = {
    "timeZone": ("time_zone", "TZ"),
    "cookieSecure": ("cookie_secure", "COOKIE_SECURE"),
    "activityLogRetentionDays": ("activity_log_retention_days", "ACTIVITY_LOG_RETENTION_DAYS"),
    "accessLogRetentionDays": ("access_log_retention_days", "ACCESS_LOG_RETENTION_DAYS"),
}
FIELDS = tuple(ENV_FIELDS)
DEFAULTS: dict[str, Any] = {name: Settings.model_fields[attr].default for name, (attr, _env) in ENV_FIELDS.items()}
# COOKIE_SECURE's field default is None (not set); the setting itself is off by default.
DEFAULTS["cookieSecure"] = False
FALLBACK_TIME_ZONE = "UTC"


class InvalidSettings(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class Stored:
    version: int
    values: dict[str, Any]  # every name in FIELDS; None = never saved
    actor: str | None = None
    ip_address: str | None = None
    created_at: Any = None


EMPTY = Stored(0, {name: None for name in FIELDS})


@dataclass(frozen=True)
class Effective:
    version: int
    time_zone: str
    cookie_secure: bool
    activity_log_retention_days: int
    access_log_retention_days: int

    def value(self, name: str) -> Any:
        return getattr(self, ENV_FIELDS[name][0])


DEFAULT_EFFECTIVE = Effective(0, *(DEFAULTS[name] for name in FIELDS))


# ── Validation (saving and loading use the same checks) ─────────

@lru_cache(maxsize=1)
def time_zones() -> frozenset[str]:
    # "Factory" is tzdata's placeholder for an unset zone, not a place, and browsers cannot format it.
    return frozenset(zoneinfo.available_timezones() - {"Factory"})


def _check_time_zone(value: Any) -> str:
    if type(value) is not str or value not in time_zones():
        raise InvalidSettings("timeZone", "Choose a time zone from the list.")
    return value


def _check_cookie_secure(value: Any) -> bool:
    if type(value) is not bool:
        raise InvalidSettings("cookieSecure", "Secure session cookie must be on or off.")
    return value


def _retention_check(name: str, label: str):
    def check(value: Any) -> int:
        if type(value) is not int or not RETENTION_MIN <= value <= RETENTION_MAX:
            raise InvalidSettings(name, f"{label} retention must be a whole number of days from {RETENTION_MIN} to {RETENTION_MAX:,}.")
        return value
    return check


CHECKS = {
    "timeZone": _check_time_zone,
    "cookieSecure": _check_cookie_secure,
    "activityLogRetentionDays": _retention_check("activityLogRetentionDays", "Activity log"),
    "accessLogRetentionDays": _retention_check("accessLogRetentionDays", "Access log"),
}


def check(name: str, value: Any) -> Any:
    return CHECKS[name](value)


def parse_value(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        raise InvalidSettings("value", "The stored value is not valid JSON.") from e
    if (
        not isinstance(data, dict)
        or type(data.get("format")) is not int
        or data["format"] != FORMAT
        or set(data) != {"format", *FIELDS}
    ):
        raise InvalidSettings("value", f"The stored value is not in format {FORMAT}.")
    return {name: None if data[name] is None else check(name, data[name]) for name in FIELDS}


def dump_value(values: dict[str, Any]) -> str:
    return json.dumps({"format": FORMAT, **{name: values[name] for name in FIELDS}}, sort_keys=True)


# ── Environment and effective values ────────────────────────────

def locked_fields() -> dict[str, bool]:
    """Fields set by the environment (or .env): pydantic records which fields its sources set.
    An empty COOKIE_SECURE reads as None, which is not set (an empty TZ stays set)."""
    s = get_settings()
    explicitly_set = s.model_fields_set
    return {
        name: attr in explicitly_set and getattr(s, attr) is not None
        for name, (attr, _env) in ENV_FIELDS.items()
    }


def env_time_zone_invalid() -> bool:
    """TZ is set but empty or not a known zone: dates stay in UTC."""
    return locked_fields()["timeZone"] and get_settings().time_zone.strip() not in time_zones()


def env_value(name: str) -> Any:
    """The valid value the environment sets for this field, or None (unset, or an invalid TZ)."""
    if not locked_fields()[name]:
        return None
    value = getattr(get_settings(), ENV_FIELDS[name][0])
    if name == "timeZone":
        value = value.strip()
        return value if value in time_zones() else None
    return value


def effective(stored: Stored) -> Effective:
    locked = locked_fields()
    values = []
    for name in FIELDS:
        if locked[name]:
            value = env_value(name)
            values.append(FALLBACK_TIME_ZONE if value is None else value)
        else:
            saved = stored.values[name]
            values.append(DEFAULTS[name] if saved is None else saved)
    return Effective(stored.version, *values)


_current: Effective = DEFAULT_EFFECTIVE


def current() -> Effective:
    """The values this process uses.  Before the first load: the defaults."""
    return _current


def apply(values: Effective) -> bool:
    """Use `values` unless this process already uses a newer version."""
    global _current
    if values.version < _current.version:
        return False
    _current = values
    return True


# ── Database ────────────────────────────────────────────────────

_SELECT_CURRENT = (
    "SELECT version, value, actor, ip_address, created_at FROM app_settings "
    "ORDER BY version DESC LIMIT 1"
)


async def read_current(db: Database) -> Stored:
    row = await db.fetchrow(_SELECT_CURRENT)
    if row is None:
        return EMPTY
    try:
        values = parse_value(row["value"])
    except InvalidSettings as e:
        raise InvalidSettings(
            e.field,
            f"General settings version {row['version']} in the table app_settings are invalid: "
            f"{e.message} Insert a newer version with valid values, the same way as described in "
            "\"Repairing stored rate limit settings\" in README.md.",
        ) from e
    return Stored(int(row["version"]), values, row["actor"], row["ip_address"], row["created_at"])


async def insert_version(db: Database, version: int, value: str, actor: str, ip_address: str | None) -> bool:
    """Save `value` as `version`.  False when that version already exists (someone saved first)."""
    try:
        await db.execute(
            "INSERT INTO app_settings (version, value, actor, ip_address) VALUES ($1, $2, $3, $4)",
            version, value, actor, ip_address,
        )
    except UniqueViolationError:
        return False
    return True


async def log_change(
    db: Database, actor: str, version: int, before: dict[str, Any], after: dict[str, Any], ip_address: str | None
) -> None:
    from app.logger import log_activity

    await log_activity(
        db,
        actor=actor,
        action="settings.update",
        entity_type="settings",
        entity_name="general",
        details={"version": version, "from": dict(before), "to": dict(after)},
        ip_address=ip_address,
    )


# ── Startup, reloading ──────────────────────────────────────────

@dataclass
class ReloadStatus:
    last_ok_ms: float | None = None
    last_error: str | None = None


@dataclass
class PurgeStatus:
    state: str = "not_run"  # "ok" | "skipped" | "not_run"
    reason: str | None = None
    last_run_ms: float | None = None


status = ReloadStatus()
purge_status: dict[str, PurgeStatus] = {"activity": PurgeStatus(), "access": PurgeStatus()}


def _iso(ms: float | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def status_view() -> dict[str, Any]:
    return {"version": current().version, "lastReload": _iso(status.last_ok_ms), "lastReloadError": status.last_error}


def record_purge(kind: str, state: str, reason: str | None) -> None:
    purge_status[kind] = PurgeStatus(state, reason, time.time() * 1000)


def purge_view() -> dict[str, Any]:
    return {
        kind: {"state": p.state, "reason": p.reason, "lastRun": _iso(p.last_run_ms)}
        for kind, p in purge_status.items()
    }


def reset_state() -> None:
    """Back to the defaults, as before the first load (tests)."""
    global _current
    _current = DEFAULT_EFFECTIVE
    status.last_ok_ms = None
    status.last_error = None
    for kind in purge_status:
        purge_status[kind] = PurgeStatus()


async def sync_from_environment(db: Database, log_db: Database) -> Stored:
    """Save every valid environment value that differs from the stored one, then return
    the stored settings.  A save lost to another process is re-read and re-checked; after
    SYNC_ATTEMPTS lost saves the start fails rather than going on without the copy."""
    for attempt in range(SYNC_ATTEMPTS + 1):
        try:
            stored = await read_current(db)
        except InvalidSettings as e:
            raise RuntimeError(e.message) from None
        target = dict(stored.values)
        for name in FIELDS:
            value = env_value(name)
            if value is not None:
                target[name] = value
        if target == stored.values:
            return stored
        if attempt == SYNC_ATTEMPTS:
            break
        try:
            saved = await insert_version(db, stored.version + 1, dump_value(target), ENVIRONMENT_ACTOR, None)
        except sqlite3.OperationalError:  # another connection held the write lock too long
            saved = False
        if saved:
            try:
                await log_change(log_db, ENVIRONMENT_ACTOR, stored.version + 1, stored.values, target, None)
            except Exception as e:
                print(f"General settings version {stored.version + 1} saved from the environment, "
                      f"but the activity log entry failed ({type(e).__name__})")
        else:
            await asyncio.sleep(0.05 * (attempt + 1))
    names = ", ".join(env for name, (_attr, env) in ENV_FIELDS.items() if env_value(name) is not None)
    raise RuntimeError(
        f"Could not save the values that the environment sets ({names}) to the general settings: "
        f"other processes saved at the same time {SYNC_ATTEMPTS} times. Start the service again."
    )


async def load(db: Database, log_db: Database) -> None:
    """At startup: copy the environment values, then use the result.  Invalid stored
    settings stop the service: the intended values (log retention above all) are unknown."""
    global _current
    stored = await sync_from_environment(db, log_db)
    _current = effective(stored)
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
        status.last_error = f"Could not read the settings ({type(e).__name__})."
        return
    apply(effective(stored))
    status.last_ok_ms = time.time() * 1000
    status.last_error = None


# ── Purge rule ──────────────────────────────────────────────────

def purge_mismatch(name: str, days: int, stored: Stored) -> str | None:
    """Why this process must not purge with `days` against the latest stored version, or None.

    The retention used must equal the stored value (or, when none was saved, the default
    while the environment does not set the field): a process whose environment or loaded
    values differ from what is saved never deletes by its own value."""
    saved = stored.values[name]
    env = ENV_FIELDS[name][1]
    if saved is not None:
        if days == saved:
            return None
        expected = saved
    else:
        if days == DEFAULTS[name] and not locked_fields()[name]:
            return None
        expected = DEFAULTS[name]
    return (
        f"This process keeps {days} days, but the saved setting (version {stored.version}) is {expected} days, "
        f"so it deleted nothing. It loads saved changes within {RELOAD_INTERVAL_SEC} seconds; if {env} is set "
        "in its environment, change or remove it and restart the service."
    )
