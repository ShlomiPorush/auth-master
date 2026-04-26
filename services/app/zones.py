from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    from app.db import Database


async def allowed_area_names(db: "Database") -> list[str]:
    rows = await db.fetch("SELECT name FROM zones ORDER BY name")
    if rows:
        return [r["name"] for r in rows]
    return get_settings().allowed_areas_list
