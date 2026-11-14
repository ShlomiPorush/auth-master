"""Responses tell browsers not to show them inside a frame.

Without X-Frame-Options and a CSP frame-ancestors directive, a page on any
same-site origin can overlay the signed-in dashboard and steer an admin's
clicks. The headers come from the app itself, so the protection does not
depend on a reverse proxy. Every response that passes through the app's
middleware carries them; Starlette's plain-text 500 for an unhandled
exception is built outside it and has nothing to click.

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
_DATABASE_URL = f"sqlite:///{_TMP_DIR.name}/framing.db"
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

HTML_PAGES = [
    "/admin/tokens",
    "/admin/zones",
    "/admin/api-keys",
    "/admin/logs/activity",
    "/admin/logs/access",
    "/admin/usage",
    "/admin/settings",
    "/login",
    "/login-mfa",
    "/setup",
    "/setup-mfa",
    "/docs",
    "/redoc",
]


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


def _assert_not_frameable(res):
    assert res.headers.get("x-frame-options") == "DENY"
    assert res.headers.get("content-security-policy") == "frame-ancestors 'none'"


@pytest.mark.parametrize("path", HTML_PAGES)
def test_html_pages_refuse_framing(client, path):
    res = client.get(path)
    assert res.status_code == 200, path
    assert res.headers["content-type"].startswith("text/html")
    _assert_not_frameable(res)


def test_pages_refuse_framing_under_root_path(client, monkeypatch):
    # With ROOT_PATH set, _serve_html builds the page itself instead of a FileResponse.
    monkeypatch.setattr(client.main, "ROOT_PATH", "/auth")
    res = client.get("/login")
    assert res.status_code == 200
    assert '<base href="/auth/" />' in res.text
    _assert_not_frameable(res)


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/", 302),
        ("/health", 200),
        ("/admin/api/me", 200),
        ("/no-such-page", 404),
        ("/css/ui.css", 200),
    ],
)
def test_other_responses_refuse_framing(client, path, status):
    res = client.get(path, follow_redirects=False)
    assert res.status_code == status, path
    _assert_not_frameable(res)


def test_method_not_allowed_refuses_framing(client):
    res = client.post("/login")
    assert res.status_code == 405
    _assert_not_frameable(res)


def test_validation_error_refuses_framing(client):
    # The app's own RequestValidationError handler builds this response.
    res = client.post("/admin/api/login", json={})
    assert res.status_code == 422
    _assert_not_frameable(res)
