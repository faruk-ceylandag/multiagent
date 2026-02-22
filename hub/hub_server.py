"""hub_server.py — Multi-Agent Hub server (modular FastAPI app)."""

import os
import asyncio
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Increase default threadpool for sync endpoints (default=40, too low with many agents)
from concurrent.futures import ThreadPoolExecutor
asyncio.get_event_loop().set_default_executor(ThreadPoolExecutor(max_workers=200))

# Initialize shared state (loads config, state, starts background threads)
from hub.state import load_state, start_background_threads, MA_DIR

# Import all routers
from hub.routers import all_routers

# ── App ──
app = FastAPI(title="Multi-Agent Hub")

@app.exception_handler(Exception)
async def global_exception_handler(request: FastAPIRequest, exc: Exception):
    import logging
    logging.getLogger("hub").error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "Internal server error", "code": "INTERNAL_ERROR"}
    )

# Optional auth middleware (only active if hub_token configured)
from hub.middleware.auth import TokenAuthMiddleware
app.add_middleware(TokenAuthMiddleware)

from starlette.middleware.base import BaseHTTPMiddleware

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from hub.state import request_counts, error_counts
        path = request.url.path
        # Skip static files and websocket
        if path.startswith("/static") or path == "/ws":
            return await call_next(request)
        response = await call_next(request)
        # Count requests
        request_counts[path] = request_counts.get(path, 0) + 1
        if response.status_code >= 400:
            key = f"{path}_{response.status_code}"
            error_counts[key] = error_counts.get(key, 0) + 1
        return response

app.add_middleware(MetricsMiddleware)

# Register all routers
for router in all_routers:
    app.include_router(router)

# ── Load persisted state ──
load_state()

# ── Start background threads ──
start_background_threads()

@app.on_event("shutdown")
async def on_shutdown():
    from hub.state import request_shutdown, shutdown_save
    request_shutdown()
    shutdown_save()

# ── Dashboard static files ──
_script_dir = os.path.dirname(os.path.abspath(__file__))
_dashboard_dir = None
for d in [os.path.join(_script_dir, "dashboard"),
          os.path.join(MA_DIR, "dashboard") if MA_DIR else ""]:
    if d and os.path.isdir(d) and os.path.exists(os.path.join(d, "index.html")):
        _dashboard_dir = d
        break

if _dashboard_dir:
    app.mount("/static", StaticFiles(directory=_dashboard_dir), name="static")

@app.get("/")
def root_page():
    if _dashboard_dir:
        return FileResponse(os.path.join(_dashboard_dir, "index.html"), media_type="text/html",
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return HTMLResponse("<h1>Dashboard not found</h1>")

# Force no-cache on static assets during development
from starlette.middleware.base import BaseHTTPMiddleware as _BHTTPM
class _NoCacheStatic(_BHTTPM):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
app.add_middleware(_NoCacheStatic)
