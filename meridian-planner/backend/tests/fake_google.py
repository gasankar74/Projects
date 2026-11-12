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
        self.writes: list[tuple[str, str]] = []  # (method, path) of every write request
        self.channels: dict[str, dict] = {}  # channel id -> {calendar, token, resourceId, address}
        self.process_then_fail: list[tuple[str, int]] = []  # (method, status): do the write, then return an error

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
        if url.netloc == "oauth2.googleapis.com" and path == "/revoke":
            tok = parse_qs(request.content.decode()).get("token", [""])[0]
            self.revoked.add(tok)
            self.revoked_tokens = getattr(self, "revoked_tokens", []) + [tok]
            return self._json(200, {})
        if url.netloc == "openidconnect.googleapis.com":
            sub = self._sub_for(request)
            ident = next(i for i in self.identities.values() if i["sub"] == sub)
            return self._json(200, ident)

        sub = self._sub_for(request)
        if sub is None:
            return self._json(401, {"error": "unauthorized"})
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            return self._write(request, path, url, sub)
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
                if "privateExtendedProperty" in q:
                    k, v = q["privateExtendedProperty"][0].split("=", 1)
                    items = [e for e in items if ((e.get("extendedProperties") or {}).get("private") or {}).get(k) == v]
                    return self._json(200, {"items": items})
            self.changes[cal_id] = []
            return self._json(200, {"items": items, "nextSyncToken": f"st:{cal_id}:{len(self.requests)}"})
        return self._json(404, {"error": "not found"})

    # ----- writes -----

    def _cal_owner_role(self, sub: str, cal_id: str) -> str | None:
        for c in self.calendars.get(sub, []):
            if c["id"] == cal_id:
                return c["accessRole"]
        return None

    def _write(self, request: httpx.Request, path: str, url, sub: str) -> httpx.Response:
        self.writes.append((request.method, path))
        body = json.loads(request.content or b"{}")
        if path == "/calendar/v3/channels/stop":
            self.channels.pop(body.get("id"), None)
            return httpx.Response(204)
        rest = path[len("/calendar/v3/calendars/"):]
        cal_id, _, tail = rest.partition("/events")
        if tail != "/watch" and self._cal_owner_role(sub, cal_id) not in ("owner", "writer"):
            return self._json(403, {"error": {"code": 403, "message": "Forbidden: read-only calendar"}})
        events = self.events.setdefault(cal_id, {})
        fail = None
        for i, (method, status) in enumerate(self.process_then_fail):
            if method == request.method:
                fail = self.process_then_fail.pop(i)[1]
                break
        if tail == "/watch":
            rid = f"res-{cal_id}"
            self.channels[body["id"]] = {"calendar": cal_id, "token": body.get("token"), "resourceId": rid, "address": body["address"]}
            return self._json(200, {"id": body["id"], "resourceId": rid, "expiration": "4102444800000"})
        if request.method == "POST" and tail == "":
            eid = body.get("id")
            if eid in events:
                return self._json(409, {"error": {"code": 409, "message": "The requested identifier already exists."}})
            ev = {**body, "status": "confirmed", "etag": f'"{len(self.requests)}"'}
            events[eid] = ev
            self.changes[cal_id].append(eid)
            resp = self._json(200, ev)
        else:
            eid = tail.lstrip("/")
            ev = events.get(eid)
            if request.method == "DELETE":
                if ev is None:
                    return self._json(404, {"error": {"code": 404, "message": "Not Found"}})
                if ev.get("status") == "cancelled":
                    return self._json(410, {"error": {"code": 410, "message": "Resource has been deleted"}})
                events[eid] = {"id": eid, "status": "cancelled", "extendedProperties": ev.get("extendedProperties")}
                self.changes[cal_id].append(eid)
                resp = httpx.Response(204)
            else:  # PATCH
                if ev is None:
                    return self._json(404, {"error": {"code": 404, "message": "Not Found"}})
                ev = {**ev, **body, "etag": f'"{len(self.requests)}"'}
                events[eid] = ev
                self.changes[cal_id].append(eid)
                resp = self._json(200, ev)
        if fail:
            return self._json(fail, {"error": {"code": fail, "message": "injected failure after processing"}})
        return resp

    def live_events(self, cal_id: str) -> list[dict]:
        return [e for e in self.events.get(cal_id, {}).values() if e.get("status") != "cancelled"]

    def user_moves(self, cal_id: str, eid: str, start, end):
        ev = self.events[cal_id][eid]
        ev["start"] = {"dateTime": start.isoformat()}
        ev["end"] = {"dateTime": end.isoformat()}
        self.changes[cal_id].append(eid)
