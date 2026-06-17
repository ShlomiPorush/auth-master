import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.config import get_settings
from app.datetime_utils import is_expired
from app.db import create_database
from app.routers import admin_api_keys, admin_auth, admin_bootstrap, admin_tokens, admin_zones, health, tokens, validate, admin_logs
from app.schema import ensure_database_schema
from app.token_cache import cache_delete
from app.logger import purge_old_logs

STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "public"

_settings = get_settings()
# Normalise: strip trailing slash, ensure leading slash if non-empty
_root = _settings.root_path.strip("/")
ROOT_PATH = f"/{_root}" if _root else ""


async def cleanup_expired_tokens(db, r) -> None:
    try:
        if db.is_sqlite:
            rows = await db.fetch("SELECT id, token_hash, expires_at FROM tokens WHERE expires_at IS NOT NULL")
            expired_ids = []
            expired_hashes = []
            for row in rows:
                if is_expired(row["expires_at"]):
                    expired_ids.append(row["id"])
                    expired_hashes.append(row["token_hash"])
            
            if expired_ids:
                placeholders = ", ".join(f"${i+1}" for i in range(len(expired_ids)))
                await db.execute(f"DELETE FROM tokens WHERE id IN ({placeholders})", *expired_ids)
                for h in expired_hashes:
                    await cache_delete(r, h)
        else:
            rows = await db.fetch("SELECT token_hash FROM tokens WHERE expires_at < now()")
            if rows:
                expired_hashes = [row["token_hash"] for row in rows]
                await db.execute("DELETE FROM tokens WHERE expires_at < now()")
                for h in expired_hashes:
                    await cache_delete(r, h)
    except Exception as e:
        print(f"Error in cleanup_expired_tokens: {e}")

    try:
        s = get_settings()
        await purge_old_logs(db, s.activity_log_retention_days, s.access_log_retention_days)
    except Exception as e:
        print(f"Error in purge_old_logs: {e}")


async def expired_tokens_cleanup_loop(db, r) -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await cleanup_expired_tokens(db, r)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error in expired_tokens_cleanup_loop: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.db = await create_database(s.database_url)
    await ensure_database_schema(app.state.db)
    app.state.redis = redis.from_url(s.redis_url, decode_responses=True)
    
    # Start cleanup background task
    cleanup_task = asyncio.create_task(
        expired_tokens_cleanup_loop(app.state.db, app.state.redis)
    )
    app.state.cleanup_task = cleanup_task
    # Trigger one cleanup immediately in the background
    asyncio.create_task(cleanup_expired_tokens(app.state.db, app.state.redis))
    
    yield
    
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
        
    await app.state.redis.aclose()
    await app.state.db.close()


app = FastAPI(title="Auth token service", root_path=ROOT_PATH, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url=f"{ROOT_PATH}/js/swagger-ui-bundle.js",
        swagger_css_url=f"{ROOT_PATH}/css/swagger-ui.css",
    )


@app.get("/redoc", include_in_schema=False)
async def custom_redoc_html():
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=app.title + " - ReDoc",
        redoc_js_url=f"{ROOT_PATH}/js/redoc.standalone.js",
    )


app.include_router(health.router)
app.include_router(validate.router)
app.include_router(tokens.router)
app.include_router(admin_bootstrap.router)
app.include_router(admin_auth.router)
app.include_router(admin_tokens.router)
app.include_router(admin_zones.router)
app.include_router(admin_api_keys.router)
app.include_router(admin_logs.router)


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
    # Convert absolute paths to relative so <base> can resolve them
    for prefix in ('"/js/', '"/css/', '"/images/'):
        html = html.replace(prefix, prefix[0] + prefix[2:])  # '"/js/' -> '"js/'
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


@app.get("/css/{filename}")
async def static_css(filename: str):
    if filename not in ("shared.css", "tailwind.css", "swagger-ui.css", "inter.css"):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        _safe_static_file(f"css/{filename}"),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/js/{filename}")
async def static_js(filename: str):
    if filename not in ("common.js", "theme.js", "head.js", "swagger-ui-bundle.js", "redoc.standalone.js", "qrcode.min.js"):
        raise HTTPException(status_code=404, detail="Not found")

    # head.js is served dynamically with BASE_PATH injected
    if filename == "head.js":
        source = _safe_static_file("js/head.js").read_text(encoding="utf-8")
        script = f'window.__BASE_PATH__ = "{ROOT_PATH}";\n{source}'
        return Response(content=script, media_type="application/javascript")

    return FileResponse(_safe_static_file(f"js/{filename}"))


@app.get("/fonts/{filename}")
async def static_fonts(filename: str):
    if not filename.endswith(".woff2") or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        _safe_static_file(f"fonts/{filename}"),
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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
