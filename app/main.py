"""Application entrypoint: wiring, lifespan, static UI and optional auth."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import api, config, db, monitor, sources
from .providers import musicbrainz, spotify

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
        await musicbrainz.aclose()
        await spotify.aclose()


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Optional shared-token gate, enabled by setting HRM_AUTH_TOKEN."""
    token = config.AUTH_TOKEN
    if not token:
        return await call_next(request)

    # The OAuth callback is reached from Spotify's servers, and the health probe
    # is used by Docker itself; neither can carry the token.
    if request.url.path in ("/api/spotify/callback", "/healthz"):
        return await call_next(request)

    supplied = (
        request.headers.get("X-Auth-Token")
        or request.query_params.get("token")
        or request.cookies.get("hrm_token")
        or ""
    )
    if supplied != token:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return Response(_login_page(), media_type="text/html", status_code=401)

    response = await call_next(request)
    if request.query_params.get("token") == token:
        response.set_cookie("hrm_token", token, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 365)
    return response


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
