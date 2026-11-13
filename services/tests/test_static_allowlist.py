"""Only allowlisted stylesheets and scripts are served from /css and /js.

Every secret the app reads is generated per run with ``secrets``; the app opens
this run's own temporary SQLite database and Redis is an in-memory fake.
Requires pytest and httpx.
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

_SECRET_FIELDS = ("admin_api_key", "session_secret", "app_encryption_key")
_PLACEHOLDERS = {
    "",
    "generate-a-long-random-string",
    "at-least-32-characters-long-secret-key",
    "min-32-chars-secret-change-me!!",
    "change-me-in-production",
    "0123456789abcdef0123456789abcdef",
} | {Settings.model_fields[f].default for f in _SECRET_FIELDS}

# Removed when the test process exits.
_TMP_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/static.db"
_SECRETS = {
    "ADMIN_API_KEY": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "APP_ENCRYPTION_KEY": secrets.token_hex(16),
}
for _name, _value in _SECRETS.items():
    if _value in _PLACEHOLDERS:
        raise RuntimeError(f"{_name} is empty or a placeholder")
os.environ.update(_SECRETS)
os.environ["DATABASE_URL"] = _DATABASE_URL
os.environ["REDIS_URL"] = f"redis://:{secrets.token_hex(32)}@127.0.0.1:6379/0"

EXPECTED_CSS = {"ui.css", "swagger-ui.css"}
EXPECTED_JS = {
    "common.js",
    "theme.js",
    "head.js",
    "ui.js",
    "dashboard.js",
    "auth-pages.js",
    "swagger-ui-bundle.js",
    "redoc.standalone.js",
    "qrcode.min.js",
}


class FakeRedis:
    async def get(self, k):
        return None

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.config import get_settings

    s = get_settings()
    for field in _SECRET_FIELDS:
        if getattr(s, field) in _PLACEHOLDERS:
            raise RuntimeError(f"{field} is empty or a placeholder")
    create_database = main_mod.create_database
    monkeypatch.setattr(main_mod, "create_database", lambda _url: create_database(_DATABASE_URL))
    monkeypatch.setattr(main_mod.redis, "from_url", lambda *a, **k: FakeRedis())

    async def _no_cleanup(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "cleanup_expired_tokens", _no_cleanup)
    with TestClient(main_mod.app) as c:
        c.main = main_mod
        yield c


def test_allowlists_are_exactly_the_expected_files(client):
    assert set(client.main.STATIC_CSS) == EXPECTED_CSS
    assert set(client.main.STATIC_JS) == EXPECTED_JS


def test_listed_files_on_disk_are_served(client):
    static = client.main.STATIC_DIR
    served = 0
    for folder, names in (("css", EXPECTED_CSS), ("js", EXPECTED_JS)):
        for name in sorted(names):
            if not (static / folder / name).is_file():
                continue
            res = client.get(f"/{folder}/{name}")
            assert res.status_code == 200, f"/{folder}/{name}"
            served += 1
    assert served > 0


def test_unlisted_names_are_not_served(client):
    static = client.main.STATIC_DIR
    unlisted = {
        "css": {"shared.css", "tailwind.css", "inter.css", "missing.css", "ui.js"},
        "js": {"missing.js", "ui.css", "..%2Fhtml.js"},
    }
    # Files that exist on disk but are not allowlisted are not served either.
    for folder, allowed in (("css", EXPECTED_CSS), ("js", EXPECTED_JS)):
        if (static / folder).is_dir():
            unlisted[folder] |= {p.name for p in (static / folder).iterdir() if p.is_file() and p.name not in allowed}
    for folder, names in unlisted.items():
        for name in sorted(names):
            assert client.get(f"/{folder}/{name}").status_code == 404, f"/{folder}/{name}"


def test_legacy_admin_js_redirects(client):
    root = client.main.ROOT_PATH
    for name in ("common.js", "theme.js"):
        res = client.get(f"/admin/js/{name}", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == f"{root}/js/{name}"
    assert client.get("/admin/js/ui.js", follow_redirects=False).status_code == 404


def test_fonts_serve_woff2_only(client):
    assert client.get("/fonts/licence.txt").status_code == 404
    assert client.get("/fonts/missing.woff2").status_code == 404


@pytest.mark.parametrize("path", ["/css/ui.css", "/js/ui.js", "/js/dashboard.js", "/js/head.js"])
def test_stylesheets_and_scripts_are_revalidated_on_every_load(client, path):
    # Their names do not change between releases, so a cached copy must not
    # outlive an upgrade and mix old styles or scripts with new markup.
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
