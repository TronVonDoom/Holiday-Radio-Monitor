"""Application entrypoint: wiring, lifespan, static UI and optional auth."""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles

from . import api, config, db, monitor, providers, sources

# Seeded on first run so the app is useful the moment the container starts.
# Every other station on the network is one click away via Stations -> Discover.
SEED_STATION = {
    "slug": "halloween-radio-main",
    "name": "Halloween Radio Main",
    "holiday": "halloween",
    "azuracast_base": "https://radio1.streamserver.link",
    "azuracast_shortcode": "halloween_radio_main",
    "icy_url": "https://radio1.streamserver.link:8000/hrm-aac",
}


def seed_stations() -> None:
    if db.query_one("SELECT id FROM stations LIMIT 1") is not None:
        return
    db.execute(
        "INSERT INTO stations (slug, name, holiday, enabled, azuracast_base, "
        "azuracast_shortcode, icy_url, created_at) VALUES (?,?,?,1,?,?,?,?)",
        (SEED_STATION["slug"], SEED_STATION["name"], SEED_STATION["holiday"],
         SEED_STATION["azuracast_base"], SEED_STATION["azuracast_shortcode"],
         SEED_STATION["icy_url"], db.now()),
    )
    db.log_event("Seeded Halloween Radio Main", source="setup")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    seed_stations()
    db.log_event(f"{config.APP_NAME} {config.APP_VERSION} started", source="setup")
    monitor.start()
    try:
        yield
    finally:
        await monitor.stop()
        await sources.aclose()
        # Every provider holds its own connection pool; closing them off the
        # registry means a new catalogue is shut down properly the day it is
        # added rather than leaking sockets until somebody notices.
        for provider in providers.REGISTRY:
            await provider.module.aclose()


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _token_matches(supplied: str) -> bool:
    """Constant-time comparison, so a wrong token gives nothing away by timing.

    `==` on strings returns as soon as two bytes differ, which leaks the length
    of the matching prefix to anyone who can time the response. That is a slow
    attack over a network and a real one on a LAN.
    """
    if not supplied:
        return False
    return secrets.compare_digest(
        supplied.encode("utf-8"), config.AUTH_TOKEN.encode("utf-8")
    )


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Optional shared-token gate, enabled by setting HRM_AUTH_TOKEN."""
    if not config.AUTH_TOKEN:
        return await call_next(request)

    # The OAuth callback is reached from Spotify's servers, and the health probe
    # is used by Docker itself; neither can carry the token.
    if request.url.path in ("/api/spotify/callback", "/healthz"):
        return await call_next(request)

    # A token in the query string is how the login form submits, so it has to be
    # accepted — but only once. It is exchanged for the cookie and the user is
    # bounced to the same address without it, because a URL is the one place a
    # credential is guaranteed to be written down: browser history, the referrer
    # header, and the access log of every proxy between here and the user.
    if _token_matches(request.query_params.get("token", "")):
        clean = request.url.remove_query_params("token")
        response = RedirectResponse(str(clean), status_code=303)
        response.set_cookie("hrm_token", config.AUTH_TOKEN, httponly=True,
                            samesite="lax", max_age=COOKIE_MAX_AGE)
        return response

    supplied = (
        request.headers.get("X-Auth-Token")
        or request.cookies.get("hrm_token")
        or ""
    )
    if not _token_matches(supplied):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return Response(_login_page(), media_type="text/html", status_code=401)

    return await call_next(request)


def _login_page() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Locked</title>"
        "<style>body{font-family:system-ui;background:#0d0a14;color:#e8e3f0;"
        "display:grid;place-items:center;height:100vh;margin:0}"
        "form{display:flex;gap:.5rem}input,button{padding:.7rem 1rem;border-radius:.6rem;"
        "border:1px solid #3a3350;background:#1a1626;color:inherit;font-size:1rem}"
        "button{background:#ff7518;color:#1a1626;font-weight:700;cursor:pointer}</style>"
        "<form onsubmit=\"location.search='?token='+encodeURIComponent(this.t.value);return false\">"
        "<input name=t placeholder='Access token' autofocus>"
        "<button>Unlock</button></form>"
    )


app.include_router(api.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": config.APP_VERSION}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(config.WEB_DIR / "icon.png", media_type="image/png")


app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info",
                access_log=False)


if __name__ == "__main__":
    run()
