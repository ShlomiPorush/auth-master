"""A SQLite database that cannot be set up must not keep the process alive.

Bug: create_database left the aiosqlite connection open when its first PRAGMA
failed (for example on a file the service user can read but not write). The
connection's thread kept the interpreter alive, so uvicorn logged "Application
startup failed. Exiting." and then hung instead of exiting, and the container
stayed up without serving.

The test runs the failing start in a separate interpreter and requires it to exit.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    import aiosqlite
    from app.db import create_database

    real_execute = aiosqlite.Connection.execute

    async def failing_execute(self, sql, *args, **kwargs):
        if sql.startswith("PRAGMA journal_mode"):
            raise aiosqlite.OperationalError("attempt to write a readonly database")
        return await real_execute(self, sql, *args, **kwargs)

    aiosqlite.Connection.execute = failing_execute
    try:
        asyncio.run(create_database("sqlite:///" + sys.argv[1]))
    except aiosqlite.OperationalError:
        print("raised")
    """
)


def test_a_failed_sqlite_setup_lets_the_process_exit(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path / "auth.db")],
        cwd=SERVICES_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "raised", result.stderr
    assert result.returncode == 0, result.stderr
