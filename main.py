"""MAVE API. Run: uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, admin, auth
from .api import router
from .core import logsetup, metrics
from .core.config import settings
from .core.errors import ProviderError
from .core.ratelimit import build_limiter
from .db import init_db

logsetup.configure(settings.env)
access_log = logging.getLogger("mave.access")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # static web app + admin dashboard, served at /web/

if settings.is_production:  # fail fast instead of running with guessable secrets
    _problems = settings.validate()
    if _problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(_problems))
    for _w in settings.warnings():
        print("WARNING:", _w)

_docs = {} if not settings.is_production else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="MAVE API", version=__version__, **_docs)
init_db()

# ------------------------------------------------------------------ rate limiting
limiter = build_limiter(settings.redis_url)  # Redis (shared by all workers) when MAVE_REDIS_URL is set, else in-process


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    if settings.trust_proxy:
        ip = request.headers.get("x-forwarded-for", ip).split(",")[0].strip()
    limit = settings.rate_limit_per_min
    if "/auth/" in request.url.path or request.url.path.endswith("/me/delete"):
        ip, limit = "auth:" + ip, settings.auth_rate_limit_per_min
    allowed = await run_in_threadpool(limiter.allow, ip, limit) if limiter.blocking else limiter.allow(ip, limit)
    if not allowed:
        return JSONResponse({"detail": "Too many requests"}, status_code=429, headers={"Retry-After": "30"})
    return await call_next(request)


@app.exception_handler(ProviderError)
async def provider_error(_: Request, exc: ProviderError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


WEB_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; "
    "connect-src 'self'; font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def observe(request: Request, call_next):
    """Request metrics + one structured access-log line. Logs the route template, never the query string."""
    started = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        elapsed = time.perf_counter() - started
        route = getattr(request.scope.get("route"), "path", None)
        if route is None:
            route = "/web/*" if request.url.path.startswith("/web") else "unmatched"
        metrics.observe(request.method, route, status, elapsed)
        if route != "/health":
            access_log.info("request", extra={"method": request.method, "route": route, "status": status, "ms": round(elapsed * 1000, 1)})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    path = request.url.path
    if path == "/web" or path.startswith("/web/"):  # the web app: own scripts/styles only, https images, same-origin API
        resp.headers.setdefault("Content-Security-Policy", WEB_CSP)
    elif path not in ("/docs", "/redoc"):  # Swagger/ReDoc (development only) need to load their own assets
        resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    if "/auth/" in path or "/me" in path or "/admin/" in path:
        resp.headers.setdefault("Cache-Control", "no-store")
    if settings.is_production:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


if settings.cors_origins:  # web client origins, e.g. MAVE_CORS_ORIGINS=https://app.example.org
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )


@app.exception_handler(admin.AdminError)
async def admin_error(_: Request, exc: admin.AdminError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.exception_handler(auth.AuthError)
async def auth_error(_: Request, exc: auth.AuthError):
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=headers)


# ------------------------------------------------------------------ mount
app.include_router(router, prefix="/api/v1")
app.include_router(router, include_in_schema=False)  # legacy un-prefixed paths for MAVE <= 0.2 clients

# Web app (phase 8) + admin dashboard (phase 9): plain HTML/CSS/JS, no build step, same origin as the API.
if WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/web/")
