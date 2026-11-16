"""Child process of test_database_move's kill tests (not a test module).

Runs a service on the SQLite file in the given working directory, starts a move to the
PostgreSQL database in MOVE_KILL_PG (the connection fields, JSON), and ends the process
with os._exit(9) right after the named point, without any cleanup, as a crash would:

  after_commit   the PostgreSQL transaction committed; the old file is not fenced yet
  after_fence    the old SQLite file is fenced; data/database.json still names SQLite
  after_publish  data/database.json names PostgreSQL; the copy is not activated yet
  after_activate the copy is active; the service has not attached it yet

The points are reached by wrapping the functions the move calls (no hook in the service).
Exit code 9: killed at the point; 3: the move ended without reaching it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def main() -> None:
    point, workdir = sys.argv[1], Path(sys.argv[2])
    os.chdir(workdir)

    import pytest
    from test_database_setup import fresh_code, service, setup_sqlite

    from app import db_config, db_move, installation
    from app.config import get_settings

    mp = pytest.MonkeyPatch()
    mp.setattr(get_settings(), "database_url", None)
    mp.setattr(db_config, "DATA_DIR", workdir / "data")

    def die() -> None:
        sys.stdout.flush()
        os._exit(9)

    real_fence, real_publish, real_activate = db_move.fence_file, db_config.publish_config, installation.activate

    def fence(*a):
        if point == "after_commit":
            die()
        result = real_fence(*a)
        if point == "after_fence":
            die()
        return result

    def publish(cfg):
        real_publish(cfg)
        if point == "after_publish" and cfg.backend == "postgres":
            die()

    async def activate(*a):
        await real_activate(*a)
        if point == "after_activate":
            die()

    mp.setattr(db_move, "fence_file", fence)
    mp.setattr(db_config, "publish_config", publish)
    mp.setattr(installation, "activate", activate)

    async def go() -> None:
        async with service(mp) as svc:
            await setup_sqlite(svc)
            admin_id, totp = await svc.add_admin()
            c = await svc.signed_in(admin_id)
            res = await c.post("/admin/api/zones", json={"name": "alpha"})
            assert res.status_code == 201, res.text
            res = await c.post("/admin/api/tokens", json={"name": "t", "grants": [{"area": "alpha", "level": "read"}]})
            assert res.status_code == 201, res.text
            body = {"postgres": json.loads(os.environ["MOVE_KILL_PG"]), "code": fresh_code(totp)}
            res = await c.post("/admin/api/settings/database/move", json=body)
            assert res.status_code == 202, res.text
            job = svc.runtime.jobs.get(res.json()["job"]["id"])
            for _ in range(2400):
                if job.state != "running":
                    break
                await asyncio.sleep(0.05)
            print(f"move ended without reaching {point}: {job.state} {job.error_code}")

    asyncio.run(go())
    sys.stdout.flush()
    os._exit(3)


if __name__ == "__main__":
    main()
