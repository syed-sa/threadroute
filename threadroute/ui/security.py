"""Web security middleware: bearer-token auth, rate limiting, request-size limits, security headers."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse

PUBLIC_PREFIXES = ("/static/", "/healthz")
PUBLIC_EXACT = {"/", "/favicon.ico"}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

CHAT_LIMIT_PER_MIN = 20
API_LIMIT_PER_MIN = 240
MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 9 * 1024 * 1024


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, int(window - (now - q[0])))
        q.append(now)
        if len(self._hits) > 5000:  # bounded memory
            for k in list(self._hits)[:2500]:
                self._hits.pop(k, None)
        return True, 0


def token_ok(header: str | None, expected: str) -> bool:
    if not header or not header.lower().startswith("bearer "):
        return False
    given = header[7:].strip()
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def install(app, expected_token: str | None, limiter: RateLimiter | None = None) -> None:
    limiter = limiter or RateLimiter()

    @app.middleware("http")
    async def guard(request, call_next):
        path = request.url.path
        public = path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)
        if not public:
            if expected_token is not None and not token_ok(request.headers.get("authorization"), expected_token):
                resp = JSONResponse({"detail": "Authentication required."}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                return _finish(resp, path)
            client = (request.client.host if request.client else "?") + ":" + (
                hashlib.sha256((request.headers.get("authorization") or "").encode()).hexdigest()[:8])
            is_chat = path.startswith("/api/chat")
            ok, retry = limiter.allow(client + (":chat" if is_chat else ""),
                                      CHAT_LIMIT_PER_MIN if is_chat else API_LIMIT_PER_MIN)
            if not ok:
                return _finish(JSONResponse({"detail": "Too many requests."}, status_code=429,
                                            headers={"Retry-After": str(retry)}), path)
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                declared = request.headers.get("content-length")
                cap = MAX_UPLOAD_BYTES if is_chat else MAX_JSON_BYTES
                if declared is None and request.method == "POST":
                    return _finish(JSONResponse({"detail": "Content-Length required."}, status_code=411), path)
                if declared is not None and (not declared.isdigit() or int(declared) > cap):
                    return _finish(JSONResponse({"detail": "Request body too large."}, status_code=413), path)
        return _finish(await call_next(request), path)


def _finish(resp, path: str):
    for k, v in HEADERS.items():
        resp.headers[k] = v
    if path.startswith("/api") or path.startswith("/samples"):
        resp.headers["Cache-Control"] = "no-store"
    return resp
