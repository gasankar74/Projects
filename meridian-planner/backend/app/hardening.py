"""Request hardening: rate limits, body size cap, cross-site request check, security headers, production checks.

The API runs as one process (uvicorn) behind the web app's /api proxy, so in-memory counters are enough; a
restart resets them, which is harmless.
"""

import hashlib
import ipaddress
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import get_settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    methods: frozenset[str] | None  # None = any
    limit: int
    window_s: int
    by: str  # "user" (session, falling back to address) or "ip"


RULES = [
    # Sign-in: per address, strict.
    Rule("auth", re.compile(r"^/auth/(dev-login|google/login|google/callback)$"), None, 20, 60, "ip"),
    # Google push notifications: Google sends bursts; generous per address.
    Rule("webhook", re.compile(r"^/webhooks/"), None, 600, 60, "ip"),
    # AI calls are the most expensive thing the server does.
    Rule("ai_min", re.compile(r"^/assistant/conversations/[^/]+/messages$|^/projects/[^/]+/breakdown$"), frozenset({"POST"}), 15, 60, "user"),
    Rule("ai_hour", re.compile(r"^/assistant/conversations/[^/]+/messages$|^/projects/[^/]+/breakdown$"), frozenset({"POST"}), 200, 3600, "user"),
    Rule("import", re.compile(r"^/imports(/motion)?$|^/import/json$"), frozenset({"POST"}), 10, 60, "user"),
    Rule("export", re.compile(r"^/export/"), None, 20, 60, "user"),
    Rule("email", re.compile(r"^/notifications/(test|digest/send)$"), frozenset({"POST"}), 5, 300, "user"),
    Rule("account_delete", re.compile(r"^/account/delete$"), frozenset({"POST"}), 5, 3600, "user"),
    # Everything else.
    Rule("write", re.compile(r"^/"), frozenset({"POST", "PUT", "PATCH", "DELETE"}), 300, 60, "user"),
    Rule("read", re.compile(r"^/"), frozenset({"GET"}), 1200, 60, "user"),
]


class Limiter:
    def __init__(self):
        self.hits: dict[tuple[str, str], deque] = {}
        self.lock = threading.Lock()

    def check(self, rule: Rule, key: str, now: float) -> int | None:
        """Record a hit. Returns None if allowed, else seconds until the next one would be."""
        with self.lock:
            q = self.hits.setdefault((rule.name, key), deque())
            while q and now - q[0] >= rule.window_s:
                q.popleft()
            if len(q) >= rule.limit:
                return max(1, int(rule.window_s - (now - q[0])) + 1)
            q.append(now)
            if len(self.hits) > 50_000:  # forget idle keys
                for k in [k for k, v in self.hits.items() if not v][:10_000]:
                    self.hits.pop(k, None)
            return None

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


limiter = Limiter()


def _is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    # Trust X-Forwarded-For only from the web app's proxy (a private address), never from the internet.
    fwd = request.headers.get("x-forwarded-for")
    if fwd and _is_private(peer):
        return fwd.split(",")[0].strip()
    return peer


def _key(request: Request, by: str) -> str:
    if by == "user":
        tok = request.cookies.get(get_settings().session_cookie_name)
        if tok:
            return "s:" + hashlib.sha256(tok.encode()).hexdigest()[:24]
    return "ip:" + client_ip(request)


def _origin_ok(request: Request) -> bool:
    """Block state-changing requests that a browser sent from another site (CSRF defence on top of SameSite)."""
    if request.method in SAFE_METHODS or request.url.path.startswith("/webhooks/"):
        return True
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None  # no Origin: not a browser cross-site request; "null": sandboxed or file page
    o = urlparse(origin).netloc.lower()
    allowed = {urlparse(get_settings().frontend_url).netloc.lower()}
    for h in ("x-forwarded-host", "host"):
        if request.headers.get(h):
            allowed.add(request.headers[h].split(",")[0].strip().lower())
    return o in allowed


class HardeningMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        path = request.url.path

        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > s.max_body_bytes:
            return JSONResponse({"detail": "Request too large"}, status_code=413)

        if not _origin_ok(request):
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)

        if s.rate_limits_enabled and path != "/healthz":
            now = time.monotonic()
            for rule in RULES:
                if rule.methods is not None and request.method not in rule.methods:
                    continue
                if not rule.pattern.search(path):
                    continue
                wait = limiter.check(rule, _key(request, rule.by), now)
                if wait is not None:
                    return JSONResponse(
                        {"detail": f"Too many requests; try again in {wait} seconds"},
                        status_code=429,
                        headers={"Retry-After": str(wait)},
                    )
                # Specific rules come first; the general read/write rule applies only if no specific one did,
                # except the AI per-minute and per-hour rules, which both apply.
                if rule.name not in ("ai_min",):
                    break

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not path.startswith("/export/") and not path.startswith("/reports/time.csv"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def production_problems() -> list[str]:
    """Settings that must not be used in production. The API refuses to start if any are found."""
    s = get_settings()
    if s.env != "production":
        return []
    out = []
    if s.dev_login_enabled:
        out.append("MERIDIAN_DEV_LOGIN_ENABLED must be false")
    if not s.cookie_secure:
        out.append("MERIDIAN_COOKIE_SECURE must be true (serve the app over HTTPS)")
    if not s.token_encryption_keys.strip():
        out.append("MERIDIAN_TOKEN_ENCRYPTION_KEYS must be set")
    if not s.frontend_url.startswith("https://"):
        out.append("MERIDIAN_FRONTEND_URL must be an https:// address")
    if not s.allowed_emails.strip():
        out.append("MERIDIAN_ALLOWED_EMAILS must list the Google addresses allowed to sign in")
    if s.ai_provider == "claude" and not s.anthropic_api_key:
        out.append("MERIDIAN_AI_PROVIDER=claude needs MERIDIAN_ANTHROPIC_API_KEY")
    return out
