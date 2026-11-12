"""Google push notifications: channel upkeep, webhook authentication, worker sync on notification."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session_factory
from app.models import Calendar, WatchChannel
from app.services import push
from app.services.google import GoogleClient
from app.worker import tick

from .test_google_sync import ev, oauth

PERSONAL = "greg.personal@example.com"


@pytest.fixture
def public_url():
    s = get_settings()
    old = s.public_url
    s.public_url = "https://meridian.example.ts.net"
    yield s.public_url
    s.public_url = old


def connect(client, google):
    google.add_account("code-personal", "sub-p", PERSONAL)
    google.add_calendar("sub-p", PERSONAL, "Personal", primary=True)
    google.add_calendar("sub-p", "court@group.calendar.google.com", "Court", role="reader")
    oauth(client, "code-personal")


def upkeep(google):
    with session_factory()() as db:
        return push.ensure_channels(db, GoogleClient(transport=google.transport()))


def notify(client, channel_id, token, state="exists"):
    return client.post("/webhooks/google", headers={
        "X-Goog-Channel-ID": channel_id, "X-Goog-Channel-Token": token, "X-Goog-Resource-State": state,
        "X-Goog-Resource-ID": "res",
    })


def flagged():
    with session_factory()() as db:
        return [c.summary for c in db.scalars(select(Calendar).where(Calendar.sync_requested_at.is_not(None)))]


def test_no_public_url_means_polling_only(client, google):
    connect(client, google)
    assert upkeep(google)["opened"] == 0
    assert google.channels == {}


def test_channels_opened_and_notifications_authenticated(client, google, public_url):
    connect(client, google)
    assert upkeep(google)["opened"] == 2  # read-only calendars are watched too (they're read, never written)
    assert {c["address"] for c in google.channels.values()} == {f"{public_url}/webhooks/google"}
    with session_factory()() as db:
        stored = db.scalars(select(WatchChannel)).all()
        assert all(len(c.token_hash) == 64 for c in stored)
        assert not any(c.token_hash == ch["token"] for c in stored for ch in google.channels.values())  # hashed
    cid, ch = next((k, v) for k, v in google.channels.items() if v["calendar"] == PERSONAL)
    assert {c["summary"]: c["push_enabled"] for c in client.get("/calendars").json()} == {"Personal": True, "Court": True}

    # Wrong token, unknown channel, and the opening handshake: all answered 200, none trigger a sync.
    assert notify(client, cid, "wrong").status_code == 200
    assert notify(client, "nope", ch["token"]).status_code == 200
    assert notify(client, cid, ch["token"], state="sync").status_code == 200
    assert flagged() == []

    # A real change notification flags the calendar; the worker syncs it within a few seconds.
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    google.put_event(PERSONAL, ev("new-meeting", start, start + timedelta(hours=1)))
    assert notify(client, cid, ch["token"]).status_code == 200
    assert flagged() == ["Personal"]
    with session_factory()() as db:
        tick(db, GoogleClient(transport=google.transport()), {"synced": datetime.now(timezone.utc), "refreshed": datetime.now(timezone.utc), "generated": datetime.now(timezone.utc)},
             now=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert flagged() == []
    q = lambda d: d.isoformat().replace("+", "%2B")  # noqa: E731
    evs = client.get(f"/events?start={q(start - timedelta(hours=1))}&end={q(start + timedelta(hours=2))}").json()
    assert [e["summary"] for e in evs] == ["new-meeting"]


def test_channels_renewed_before_expiry_and_stopped_on_disconnect(client, google, public_url):
    connect(client, google)
    upkeep(google)
    old_ids = set(google.channels)
    assert upkeep(google) == {"opened": 0, "renewed": 0, "closed": 0, "errors": 0}
    with session_factory()() as db:
        for c in db.scalars(select(WatchChannel)):
            c.expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        db.commit()
    out = upkeep(google)
    assert out["renewed"] == 2 and out["closed"] == 2
    assert not (set(google.channels) & old_ids)
    acct = client.get("/google-accounts").json()[0]
    assert client.delete(f"/google-accounts/{acct['id']}").status_code == 204
    assert google.channels == {}
