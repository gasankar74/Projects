"""Thin Google OAuth + Calendar API client.

The HTTP transport can be swapped (tests use httpx.MockTransport), so sync logic is tested without network.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from ..config import get_settings

SIGNIN_SCOPES = ["openid", "email", "profile"]
CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    # Needed in Phase 3 to write task blocks; it can create and edit events but not delete calendars.
    "https://www.googleapis.com/auth/calendar.events",
]


class GoogleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google API {status}: {message}")
        self.status = status
        self.message = message


class SyncTokenExpired(GoogleError):
    pass


class TokenRevoked(GoogleError):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(state: str, code_challenge: str, login_hint: str | None = None) -> str:
    s = get_settings()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SIGNIN_SCOPES + CALENDAR_SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{s.google_auth_url}?{urlencode(params)}"


class GoogleClient:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(transport=transport, timeout=20.0)
        self.s = get_settings()

    # ----- OAuth -----

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        r = self.http.post(
            self.s.google_token_url,
            data={
                "code": code,
                "client_id": self.s.google_client_id,
                "client_secret": self.s.google_client_secret,
                "redirect_uri": self.s.google_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        if r.status_code != 200:
            raise GoogleError(r.status_code, r.text)
        return r.json()

    def refresh(self, refresh_token: str) -> dict:
        r = self.http.post(
            self.s.google_token_url,
            data={
                "client_id": self.s.google_client_id,
                "client_secret": self.s.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code == 400 and "invalid_grant" in r.text:
            raise TokenRevoked(400, "Google access was revoked or expired; reconnect this account")
        if r.status_code != 200:
            raise GoogleError(r.status_code, r.text)
        return r.json()

    def userinfo(self, access_token: str) -> dict:
        r = self.http.get(self.s.google_userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code != 200:
            raise GoogleError(r.status_code, r.text)
        return r.json()

    # ----- Calendar -----

    def _get(self, access_token: str, path: str, params: dict | None = None) -> dict:
        r = self.http.get(
            f"{self.s.google_calendar_api}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code == 410:
            raise SyncTokenExpired(410, "Sync token expired")
        if r.status_code == 401:
            raise GoogleError(401, "Access token rejected")
        if r.status_code != 200:
            raise GoogleError(r.status_code, r.text[:500])
        return r.json()

    def list_calendars(self, access_token: str) -> list[dict]:
        items, page = [], None
        while True:
            params = {"maxResults": 250, "showHidden": "false"}
            if page:
                params["pageToken"] = page
            data = self._get(access_token, "/users/me/calendarList", params)
            items += data.get("items", [])
            page = data.get("nextPageToken")
            if not page:
                return items

    # ----- writing (task blocks) -----

    def _send(self, method: str, access_token: str, path: str, json: dict | None = None, params: dict | None = None) -> httpx.Response:
        return self.http.request(
            method,
            f"{self.s.google_calendar_api}{path}",
            json=json,
            params=params or {},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    @staticmethod
    def _cal_path(calendar_id: str) -> str:
        from urllib.parse import quote

        return f"/calendars/{quote(calendar_id, safe='')}"

    def insert_event(self, access_token: str, calendar_id: str, body: dict) -> dict:
        r = self._send("POST", access_token, f"{self._cal_path(calendar_id)}/events", json=body, params={"sendUpdates": "none"})
        if r.status_code not in (200, 201):
            raise GoogleError(r.status_code, r.text[:500])
        return r.json()

    def patch_event(self, access_token: str, calendar_id: str, event_id: str, body: dict) -> dict:
        r = self._send("PATCH", access_token, f"{self._cal_path(calendar_id)}/events/{event_id}", json=body, params={"sendUpdates": "none"})
        if r.status_code != 200:
            raise GoogleError(r.status_code, r.text[:500])
        return r.json()

    def delete_event(self, access_token: str, calendar_id: str, event_id: str) -> None:
        r = self._send("DELETE", access_token, f"{self._cal_path(calendar_id)}/events/{event_id}", params={"sendUpdates": "none"})
        if r.status_code not in (200, 204, 404, 410):
            raise GoogleError(r.status_code, r.text[:500])

    def list_tagged_events(self, access_token: str, calendar_id: str, prop: str, time_min: datetime, time_max: datetime) -> list[dict]:
        """Events carrying our private extended property (for reconciliation)."""
        items, page = [], None
        while True:
            params: dict = {
                "privateExtendedProperty": prop, "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
                "singleEvents": "true", "showDeleted": "false", "maxResults": 2500,
            }
            if page:
                params["pageToken"] = page
            data = self._get(access_token, f"{self._cal_path(calendar_id)}/events", params)
            items += data.get("items", [])
            page = data.get("nextPageToken")
            if not page:
                return items

    # ----- push notifications -----

    def watch_events(self, access_token: str, calendar_id: str, channel_id: str, address: str, token: str, ttl_s: int) -> dict:
        body = {"id": channel_id, "type": "web_hook", "address": address, "token": token, "params": {"ttl": str(ttl_s)}}
        r = self._send("POST", access_token, f"{self._cal_path(calendar_id)}/events/watch", json=body)
        if r.status_code != 200:
            raise GoogleError(r.status_code, r.text[:500])
        return r.json()

    def stop_channel(self, access_token: str, channel_id: str, resource_id: str) -> None:
        r = self._send("POST", access_token, "/channels/stop", json={"id": channel_id, "resourceId": resource_id})
        if r.status_code not in (200, 204, 404):
            raise GoogleError(r.status_code, r.text[:500])

    def list_events(self, access_token: str, calendar_id: str, sync_token: str | None) -> tuple[list[dict], str | None]:
        """Return (events, next_sync_token). Full sync when sync_token is None, else incremental."""
        from urllib.parse import quote

        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        items, page = [], None
        while True:
            params: dict = {"maxResults": 2500, "singleEvents": "true", "showDeleted": "true"}
            if sync_token:
                params["syncToken"] = sync_token
            else:
                now = datetime.now(timezone.utc)
                params["timeMin"] = (now - timedelta(days=self.s.sync_past_days)).isoformat()
                params["timeMax"] = (now + timedelta(days=self.s.sync_future_days)).isoformat()
            if page:
                params["pageToken"] = page
            data = self._get(access_token, path, params)
            items += data.get("items", [])
            page = data.get("nextPageToken")
            if not page:
                return items, data.get("nextSyncToken")
