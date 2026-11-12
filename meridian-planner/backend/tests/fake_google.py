"""In-memory stand-in for Google's OAuth and Calendar v3 endpoints (enough of them for sync tests)."""

import json
from urllib.parse import parse_qs, unquote, urlparse

import httpx


class FakeGoogle:
    def __init__(self):
        # accounts keyed by authorization code -> identity
        self.identities: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> sub
        self.revoked: set[str] = set()
        self.calendars: dict[str, list[dict]] = {}  # sub -> calendarList items
        self.events: dict[str, dict[str, dict]] = {}  # calendar id -> event id -> event
        self.changes: dict[str, list[str]] = {}  # calendar id -> ids changed since the last token
        self.tokens_issued = 0
        self.fail_next: list[tuple[str, int]] = []  # (path substring, status)
        self.expire_sync_tokens: str | None = None  # calendar id whose next sync token is rejected
        self.requests: list[str] = []

    # ----- test helpers -----

    def add_account(self, code: str, sub: str, email: str, name: str = "Test"):
        self.identities[code] = {"sub": sub, "email": email, "name": name}
        self.calendars.setdefault(sub, [])

    def add_calendar(self, sub: str, cal_id: str, summary: str, role="owner", primary=False, tz="America/New_York", color="#039be5"):
        self.calendars[sub].append(
            {"id": cal_id, "summary": summary, "accessRole": role, "primary": primary, "timeZone": tz, "backgroundColor": color, "selected": True}
        )
        self.events.setdefault(cal_id, {})
        self.changes.setdefault(cal_id, [])

    def put_event(self, cal_id: str, ev: dict):
        ev = {"status": "confirmed", "etag": f'"{len(self.requests)}"', **ev}
        self.events[cal_id][ev["id"]] = ev
        self.changes[cal_id].append(ev["id"])

    def cancel_event(self, cal_id: str, ev_id: str):
        self.events[cal_id][ev_id] = {"id": ev_id, "status": "cancelled"}
        self.changes[cal_id].append(ev_id)

    def remove_silently(self, cal_id: str, ev_id: str):
        """Event disappears without a change record (as when changes expire); only a full sync notices."""
        self.events[cal_id].pop(ev_id, None)

    # ----- transport -----

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _json(self, status: int, body) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})

    def _sub_for(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        return token.split(":", 1)[1] if token.startswith("at:") else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = unquote(url.path)
        self.requests.append(path)
        for i, (needle, status) in enumerate(self.fail_next):
            if needle in path:
                self.fail_next.pop(i)
                return self._json(status, {"error": {"code": status, "message": "injected failure"}})

        if url.netloc == "oauth2.googleapis.com" and path == "/token":
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            if grant == "authorization_code":
                ident = self.identities.get(form["code"][0])
                if not ident or not form.get("code_verifier"):
                    return self._json(400, {"error": "invalid_grant"})
                rt = f"rt:{ident['sub']}:{self.tokens_issued}"
                self.tokens_issued += 1
                self.refresh_tokens[rt] = ident["sub"]
                return self._json(200, {"access_token": f"at:{ident['sub']}", "expires_in": 3600, "refresh_token": rt, "scope": "openid email"})
            if grant == "refresh_token":
                rt = form["refresh_token"][0]
                if rt in self.revoked or rt not in self.refresh_tokens:
                    return self._json(400, {"error": "invalid_grant"})
                return self._json(200, {"access_token": f"at:{self.refresh_tokens[rt]}", "expires_in": 3600})
        if url.netloc == "openidconnect.googleapis.com":
            sub = self._sub_for(request)
            ident = next(i for i in self.identities.values() if i["sub"] == sub)
            return self._json(200, ident)

        sub = self._sub_for(request)
        if sub is None:
            return self._json(401, {"error": "unauthorized"})
        if path == "/calendar/v3/users/me/calendarList":
            return self._json(200, {"items": self.calendars.get(sub, [])})
        if path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            cal_id = path[len("/calendar/v3/calendars/"):-len("/events")]
            q = parse_qs(url.query)
            if "syncToken" in q:
                if self.expire_sync_tokens == cal_id:
                    self.expire_sync_tokens = None
                    return self._json(410, {"error": {"code": 410, "message": "Sync token is no longer valid"}})
                ids = list(dict.fromkeys(self.changes[cal_id]))
                items = [self.events[cal_id].get(i) or {"id": i, "status": "cancelled"} for i in ids]
            else:
                items = [e for e in self.events[cal_id].values() if e.get("status") != "cancelled"]
            self.changes[cal_id] = []
            return self._json(200, {"items": items, "nextSyncToken": f"st:{cal_id}:{len(self.requests)}"})
        return self._json(404, {"error": "not found"})
