"""Optional bearer token authentication middleware."""

import os
import json
import hmac
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Paths that don't require auth (health check, static files, dashboard)
PUBLIC_PATHS = {"/health", "/", "/static"}


def _load_token():
    """Load hub_token from config."""
    ma_dir = os.environ.get("MA_DIR", "")
    workspace = os.environ.get("WORKSPACE", "")
    for p in [os.path.join(workspace, "multiagent.json"),
              os.path.join(ma_dir, "config.json") if ma_dir else ""]:
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                return cfg.get("hub_token", "")
            except Exception:
                pass
    return ""


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Simple bearer token auth. Disabled if no hub_token in config."""

    def __init__(self, app):
        super().__init__(app)
        self.token = _load_token()

    async def dispatch(self, request: Request, call_next):
        if not self.token:
            return await call_next(request)

        path = request.url.path
        # Allow public paths
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        # Check Authorization header (constant-time comparison)
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if hmac.compare_digest(auth.encode(), expected.encode()):
            return await call_next(request)

        # Check cookie fallback (for dashboard, constant-time comparison)
        cookie_token = request.cookies.get("hub_token", "")
        if cookie_token and hmac.compare_digest(cookie_token.encode(), self.token.encode()):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "Unauthorized", "code": "AUTH_REQUIRED"}
        )
