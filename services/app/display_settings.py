"""How dates and times are written on the admin pages: the Date Format and Time Format of
Settings > General.

Stored in the existing key-value table app_state under the one key KEY, as JSON
{format, version, dateFormat, timeFormat, actor, savedAt}.  No table of its own and no
schema change: database inspection, upgrades and downgrades are unaffected, and a released
version that does not know the key never reads it.  A missing key is version 0 with the
defaults.  A stored value that cannot be read gives the defaults and an error in the log;
it never stops the service.

A save is compare-and-set: INSERT when the key is absent (ON CONFLICT DO NOTHING), else
UPDATE ... WHERE value = <the value read>, each with RETURNING through
Database.fetchval_write.  No returned row means someone saved first (a version conflict).

Effective value of each field: the environment variable when it is set (the field is then
locked on the page; an unknown value is ignored and the default is used), else the stored
value, else the default.  At startup a valid environment value that differs from the stored
one is saved (actor "environment"), so removing the variable later keeps the last value.

Each process holds the effective values in memory (current()), loads them when it attaches a
database (also after a switch, whatever the version it held), reloads them every
RELOAD_INTERVAL_SEC seconds, and applies its own saves at once.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db import Database

KEY = "display_settings"
FORMAT = 1
ENVIRONMENT_ACTOR = "environment"
RELOAD_INTERVAL_SEC = 15  # the reload loop in app.main

# "browser" leaves the order of the date, or the clock, to the browser's language.
DATE_FORMATS = ("browser", "DD/MM/YYYY", "MM/DD/YYYY", "DD.MM.YYYY", "YYYY-MM-DD")
TIME_FORMATS = ("24h", "12h", "browser")
OPTIONS = {"dateFormat": DATE_FORMATS, "timeFormat": TIME_FORMATS}
DEFAULTS = {"dateFormat": "browser", "timeFormat": "24h"}

# Field name on the page and in the API -> Settings attribute and environment variable.
ENV_FIELDS = {
    "dateFormat": ("date_format", "DATE_FORMAT"),
    "timeFormat": ("time_format", "TIME_FORMAT"),
}
FIELDS = tuple(ENV_FIELDS)


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
    saved_at: str | None = None
    raw: str | None = None  # the text read, for the compare-and-set; None = no key
    invalid: str | None = None  # why the stored text could not be used


EMPTY = Stored(0, {name: None for name in FIELDS})


@dataclass(frozen=True)
class Effective:
    version: int
    date_format: str
    time_format: str

    def value(self, name: str) -> str:
        return self.date_format if name == "dateFormat" else self.time_format


DEFAULT_EFFECTIVE = Effective(0, DEFAULTS["dateFormat"], DEFAULTS["timeFormat"])


# ── Validation ──────────────────────────────────────────────────

_MESSAGES = {
    "dateFormat": "Choose a date format from the list.",
    "timeFormat": "Choose a time format from the list.",
}


def check(name: str, value: Any) -> str:
    if type(value) is not str or value not in OPTIONS[name]:
        raise InvalidSettings(name, _MESSAGES[name])
    return value


def parse_value(text: Any) -> tuple[int, dict[str, Any], str | None, str | None]:
    """(version, values, actor, savedAt) of a stored text, or InvalidSettings."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        raise InvalidSettings("value", "The stored value is not valid JSON.") from e
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT
        or type(data.get("format")) is not int
        or type(data.get("version")) is not int
        or data["version"] < 1
    ):
        raise InvalidSettings("value", f"The stored value is not in format {FORMAT}.")
    values = {name: check(name, data.get(name)) for name in FIELDS}
    actor = data.get("actor") if isinstance(data.get("actor"), str) else None
    saved_at = data.get("savedAt") if isinstance(data.get("savedAt"), str) else None
    return data["version"], values, actor, saved_at


def dump_value(version: int, values: dict[str, Any], actor: str, saved_at: str) -> str:
    return json.dumps(
        {"format": FORMAT, "version": version, **{name: values[name] for name in FIELDS},
         "actor": actor, "savedAt": saved_at},
        sort_keys=True,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Environment and effective values ────────────────────────────

def locked_fields() -> dict[str, bool]:
    """Fields the environment (or .env) sets; an empty value counts as not set."""
    s = get_settings()
    return {name: getattr(s, attr) is not None for name, (attr, _env) in ENV_FIELDS.items()}


def env_invalid(name: str) -> bool:
    """The variable is set to a value that is not one of the choices: the default is used."""
    return locked_fields()[name] and getattr(get_settings(), ENV_FIELDS[name][0]).strip() not in OPTIONS[name]


def env_value(name: str) -> str | None:
    """The valid value the environment sets for this field, or None."""
    if not locked_fields()[name]:
        return None
    value = getattr(get_settings(), ENV_FIELDS[name][0]).strip()
    return value if value in OPTIONS[name] else None


def effective(stored: Stored) -> Effective:
    locked = locked_fields()
    values = {}
    for name in FIELDS:
        if locked[name]:
            values[name] = env_value(name) or DEFAULTS[name]
        else:
            saved = stored.values[name]
            values[name] = DEFAULTS[name] if saved is None else saved
    return Effective(stored.version, values["dateFormat"], values["timeFormat"])


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

async def read_current(db: Database) -> Stored:
    """The stored settings.  A value that cannot be used reads as the defaults, with the reason
    in `invalid` (and its text in `raw`, so a save can replace it)."""
    text = await db.fetchval("SELECT value FROM app_state WHERE key = $1", KEY)
    if text is None:
        return EMPTY
    try:
        version, values, actor, saved_at = parse_value(text)
    except InvalidSettings as e:
        return Stored(0, {name: None for name in FIELDS}, raw=text, invalid=e.message)
    return Stored(version, values, actor, saved_at, raw=text)


def _server_refused(e: BaseException) -> bool:
    """True when PostgreSQL itself answered with an error for the statement (it did not take
    effect); False for a lost or failed connection (SQLSTATE class 08) and anything else."""
    try:
        import asyncpg
    except ImportError:  # pragma: no cover
        return False
    if not isinstance(e, asyncpg.PostgresError):
        return False
    return not str(getattr(e, "sqlstate", "") or "").startswith("08")


class WriteUnknown(Exception):
    """The connection failed before the database confirmed the write: it may have been saved."""

    def __init__(self) -> None:
        super().__init__("the database did not confirm the write")


class WriteRefused(Exception):
    """The database did not take the write (privileges, read-only, locked)."""

    def __init__(self, busy: bool) -> None:
        super().__init__("the database refused the write")
        self.busy = busy


async def save(write_db: Database, base: Stored, value: str) -> bool:
    """Replace `base` with `value`.  False when the stored value is no longer `base`.

    `write_db` is the main database handle: fetchval_write runs the statement as its own
    committed transaction and returns what RETURNING gives, on both engines."""
    try:
        if base.raw is None:
            done = await write_db.fetchval_write(
                "INSERT INTO app_state (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING RETURNING key",
                KEY, value,
            )
        else:
            done = await write_db.fetchval_write(
                "UPDATE app_state SET value = $2 WHERE key = $1 AND value = $3 RETURNING key",
                KEY, value, base.raw,
            )
    except sqlite3.OperationalError as e:
        # SQLite runs in this process: an error means the statement did not take effect
        raise WriteRefused(busy="locked" in str(e).lower()) from None
    except Exception as e:
        if _server_refused(e):
            # PostgreSQL answered with an error: no privilege, a read-only transaction or server
            print(f"Display settings were not saved: the database refused the write ({type(e).__name__})")
            raise WriteRefused(busy=False) from None
        # The connection failed before the answer came: the write may have been committed
        print(f"Display settings: the database did not confirm the write ({type(e).__name__})")
        raise WriteUnknown() from None
    return done is not None


async def log_change(
    db: Database, actor: str, version: int, before: dict[str, Any], after: dict[str, Any], ip_address: str | None
) -> None:
    from app.logger import log_activity

    await log_activity(
        db,
        actor=actor,
        action="settings.update",
        entity_type="settings",
        entity_name="display",
        details={"version": version, "from": dict(before), "to": dict(after)},
        ip_address=ip_address,
    )


# ── Startup, reloading ──────────────────────────────────────────

@dataclass
class ReloadStatus:
    last_ok_ms: float | None = None
    last_error: str | None = None


status = ReloadStatus()


def _iso(ms: float | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def status_view() -> dict[str, Any]:
    return {"version": current().version, "lastReload": _iso(status.last_ok_ms), "lastReloadError": status.last_error}


def reset_state() -> None:
    """Back to the defaults, as before the first load (tests)."""
    global _current
    _current = DEFAULT_EFFECTIVE
    status.last_ok_ms = None
    status.last_error = None


def _report_invalid(stored: Stored) -> None:
    if stored.invalid:
        print(f"The display settings stored in app_state ({KEY}) cannot be used: {stored.invalid} "
              "Dates use the default formats until they are saved again on the Settings page.")


async def sync_from_environment(db: Database, write_db: Database) -> Stored:
    """Save the valid environment values that differ from the stored ones, then return the
    stored settings.  A failure is logged and the start goes on (the environment values
    apply anyway while they are set)."""
    stored = await read_current(db)
    target = dict(stored.values)
    for name in FIELDS:
        value = env_value(name)
        if value is not None:
            target[name] = value
    if target == stored.values:
        return stored
    filled = {name: target[name] if target[name] is not None else DEFAULTS[name] for name in FIELDS}
    version = stored.version + 1
    value = dump_value(version, filled, ENVIRONMENT_ACTOR, now_iso())
    try:
        saved = await save(write_db, stored, value)
    except WriteRefused:
        saved = False
    if not saved:
        print("The display settings set by DATE_FORMAT or TIME_FORMAT were not copied to the Settings page; "
              "they apply while the variables are set.")
        return stored
    try:
        await log_change(write_db, ENVIRONMENT_ACTOR, version, stored.values, filled, None)
    except Exception as e:
        print(f"Display settings version {version} saved from the environment, but the activity log entry failed ({type(e).__name__})")
    return await read_current(db)


async def load(db: Database, write_db: Database) -> None:
    """When a database is attached (start-up and every switch): copy the environment values,
    then use what this database holds, whatever version this process used before."""
    global _current
    try:
        stored = await sync_from_environment(db, write_db)
    except Exception as e:
        print(f"Could not read the display settings ({type(e).__name__}); dates use the default formats.")
        stored = EMPTY
        status.last_error = f"Could not read the settings ({type(e).__name__})."
    else:
        status.last_error = None
        status.last_ok_ms = time.time() * 1000
    _report_invalid(stored)
    _current = effective(stored)


async def reload_once(db: Database) -> None:
    """Apply newer saved settings; on a read failure keep the current ones and record why."""
    try:
        stored = await read_current(db)
    except Exception as e:  # database unreachable or locked: keep the last good settings
        status.last_error = f"Could not read the settings ({type(e).__name__})."
        return
    if stored.invalid:
        status.last_error = stored.invalid
    else:
        status.last_error = None
    apply(effective(stored))
    status.last_ok_ms = time.time() * 1000
