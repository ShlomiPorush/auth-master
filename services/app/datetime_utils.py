"""Shared date/time formatting utilities — timezone-aware."""

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


def _get_tz() -> ZoneInfo:
    """Return the ZoneInfo for the TZ env-var (default: UTC)."""
    tz_name = os.environ.get("TZ", "UTC").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def fmt_datetime(val: Any) -> str | None:
    """Convert a DB datetime value (str or datetime) to an ISO string in the configured TZ.

    - If *val* is a string (SQLite), it is parsed as UTC and converted to the configured TZ.
    - If *val* is a datetime (Postgres), it is converted (assuming UTC if naive).
    """
    if val is None:
        return None

    tz = _get_tz()

    if isinstance(val, str):
        # SQLite stores as "YYYY-MM-DD HH:MM:SS" (UTC, no tzinfo)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                dt = datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                return dt.astimezone(tz).isoformat()
            except ValueError:
                continue
        # If it already has timezone info (e.g., from PG text representation), return as-is
        return val

    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(tz).isoformat()

    return str(val)
