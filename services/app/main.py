from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.config import get_settings
from app.db import create_database
from app.routers import admin_api_keys, admin_auth, admin_bootstrap, admin_tokens, admin_zones, health, tokens, validate
from app.schema import ensure_database_schema

STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "public"

_settings = get_settings()
# Normalise: strip trailing slash, ensure leading slash if non-empty
_root = _settings.root_path.strip("/")
ROOT_PATH = f"/{_root}" if _root else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.db = await create_database(s.database_url)
    await ensure_database_schema(app.state.db)
    app.state.redis = redis.from_url(s.redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()
    await app.state.db.close()


app = FastAPI(title="Auth token service", root_path=ROOT_PATH, lifespan=lifespan)

app.include_router(health.router)
app.include_router(validate.router)
app.include_router(tokens.router)
app.include_router(admin_bootstrap.router)
app.include_router(admin_auth.router)
app.include_router(admin_tokens.router)
app.include_router(admin_zones.router)
app.include_router(admin_api_keys.router)


def _safe_static_file(rel: str) -> Path:
    base = STATIC_DIR.resolve()
    target = (STATIC_DIR / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Not found") from e
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return target


def _prefixed(path: str) -> str:
    """Prepend ROOT_PATH to a path for redirects."""
    return f"{ROOT_PATH}{path}"


def _serve_html(rel: str) -> Response:
    """Serve an HTML file, injecting <base href> when ROOT_PATH is set."""
    p = _safe_static_file(rel)
    if not ROOT_PATH:
        return FileResponse(p)
    html = p.read_text(encoding="utf-8")
    base_tag = f'<base href="{ROOT_PATH}/" />'
    html = html.replace("<head>", f"<head>\n    {base_tag}", 1)
    return Response(content=html, media_type="text/html")


@app.get("/admin")
@app.get("/admin/")
async def legacy_admin_root():
    return RedirectResponse(_prefixed("/"), status_code=302)


@app.get("/admin/index.html")
@app.get("/admin/dashboard.html")
async def legacy_admin_dashboard_files():
    return RedirectResponse(_prefixed("/"), status_code=302)


@app.get("/admin/setup.html")
async def legacy_admin_setup():
    return RedirectResponse(_prefixed("/setup"), status_code=302)


@app.get("/admin/setup-mfa.html")
async def legacy_admin_setup_mfa():
    return RedirectResponse(_prefixed("/setup-mfa"), status_code=302)


@app.get("/admin/login.html")
async def legacy_admin_login():
    return RedirectResponse(_prefixed("/login"), status_code=302)


@app.get("/admin/login-mfa.html")
async def legacy_admin_login_mfa():
    return RedirectResponse(_prefixed("/login-mfa"), status_code=302)


@app.get("/admin/js/{filename}")
async def legacy_admin_js(filename: str):
    if filename not in ("common.js", "theme.js"):
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(_prefixed(f"/js/{filename}"), status_code=302)


@app.get("/css/tailwind.css")
async def tailwind_css():
    return FileResponse(
        _safe_static_file("css/tailwind.css"),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/css/shared.css")
async def shared_css():
    return FileResponse(
        _safe_static_file("css/shared.css"),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/js/{filename}")
async def static_js(filename: str):
    if filename not in ("common.js", "theme.js", "head.js"):
        raise HTTPException(status_code=404, detail="Not found")

    # head.js is served dynamically with BASE_PATH injected
    if filename == "head.js":
        source = _safe_static_file("js/head.js").read_text(encoding="utf-8")
        script = f'window.__BASE_PATH__ = "{ROOT_PATH}";\n{source}'
        return Response(content=script, media_type="application/javascript")

    return FileResponse(_safe_static_file(f"js/{filename}"))


@app.get("/images/{filename}")
async def static_images(filename: str):
    return FileResponse(_safe_static_file(f"images/{filename}"))


@app.get("/setup")
async def page_setup():
    return _serve_html("setup.html")


@app.get("/setup-mfa")
async def page_setup_mfa():
    return _serve_html("setup-mfa.html")


@app.get("/login")
async def page_login():
    return _serve_html("login.html")


@app.get("/login-mfa")
async def page_login_mfa():
    return _serve_html("login-mfa.html")


@app.get("/")
async def page_home():
    return _serve_html("index.html")


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=s.port)
