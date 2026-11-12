"""Notifications: generation (idempotent), in-app list, push and email delivery with throttling, quiet hours,
confidential masking, sync-failure alerts and the daily digest."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db import session_factory
from app.models import GoogleAccount, Notification, PushSubscription, User
from app.services import mailer, notify, webpush

from .conftest import login
from .test_schedule_api import NINE_FIVE, now, schedule, setup, task


@pytest.fixture
def outbox():
    sent: list = []
    mailer.set_mailer_for_tests(sent)
    yield sent
    mailer.set_mailer_for_tests(None)


@pytest.fixture
def pushes():
    sent: list = []
    webpush.set_push_for_tests(sent)
    yield sent
    webpush.set_push_for_tests(None)


def subscribe(c, endpoint="https://push.example.com/sub/1"):
    r = c.post("/push/subscribe", json={"endpoint": endpoint, "keys": {"p256dh": "BPk" + "a" * 60, "auth": "x" * 22}})
    assert r.status_code == 204, r.text


def run_notify(at: datetime | None = None):
    with session_factory()() as db:
        notify.tick(db, at or now())


def no_quiet(c):
    r = c.patch("/notifications/settings", json={"quiet_start": "", "quiet_end": ""})
    assert r.status_code == 200, r.text


def impossible(c, **kw):
    setup(c, hours=NINE_FIVE)
    t = task(c, title=kw.pop("title", "Appeal brief"), duration_min=600, deadline=(now() + timedelta(hours=3)).isoformat(), **kw)
    schedule(c)
    return t


def test_deadline_alert_in_app_push_and_email_once(authed, outbox, pushes):
    no_quiet(authed)
    subscribe(authed)
    t = impossible(authed)
    run_notify()
    items = authed.get("/notifications").json()
    missed = [n for n in items["items"] if n["kind"] == "deadline_missed"]
    assert len(missed) == 1 and items["unread"] >= 1
    n = missed[0]
    assert "Appeal brief" in n["title"] and n["task_id"] == t["id"] and n["link"] == f"/?task={t['id']}"
    assert n["channels"] == {"push": "sent", "email": "sent"}
    assert len(pushes) >= 1 and pushes[0]["payload"]["url"] == f"/?task={t['id']}"
    assert len(outbox) == 1 and "Appeal brief" in outbox[0]["Subject"] and outbox[0]["To"] == "greg@example.com"
    # Scanning again changes nothing.
    run_notify()
    run_notify()
    assert len([n for n in authed.get("/notifications").json()["items"] if n["kind"] == "deadline_missed"]) == 1
    assert len(outbox) == 1


def test_confidential_tasks_are_not_named_outside_the_app(authed, outbox, pushes):
    no_quiet(authed)
    subscribe(authed)
    impossible(authed, title="Smith divorce filing", confidential=True)
    run_notify()
    n = next(n for n in authed.get("/notifications").json()["items"] if n["kind"] == "deadline_missed")
    assert "Smith divorce filing" in n["title"]  # in the app
    assert all("Smith" not in p["payload"]["title"] + p["payload"]["body"] for p in pushes)
    assert "Smith" not in outbox[0]["Subject"] and "Smith" not in outbox[0].get_content()
    assert "confidential task" in pushes[0]["payload"]["title"]


def test_quiet_hours_and_push_throttle(authed, pushes):
    local = now().astimezone(ZoneInfo("America/New_York"))
    start = (local - timedelta(hours=1)).strftime("%H:%M")
    end = (local + timedelta(hours=1)).strftime("%H:%M")
    authed.patch("/notifications/settings", json={"quiet_start": start, "quiet_end": end})
    subscribe(authed)
    impossible(authed)
    run_notify()
    n = next(n for n in authed.get("/notifications").json()["items"] if n["kind"] == "deadline_missed")
    assert n["channels"]["push"] == "quiet_hours" and pushes == []

    # Throttle: at most 2 pushes an hour.
    no_quiet(authed)
    authed.patch("/notifications/settings", json={"push_per_hour": 2})
    with session_factory()() as db:
        user = db.scalars(select(User)).one()
        for i in range(4):
            notify.create(db, user, "deadline_risk", f"Risk {i}", dedupe_key=f"r{i}")
        notify.deliver(db, user, now())
        got = {n.title: n.channels.get("push") for n in db.scalars(select(Notification).where(Notification.kind == "deadline_risk"))}
    assert sorted(got.values()) == ["sent", "sent", "throttled", "throttled"]


def test_kind_switched_off_still_shows_in_app(authed, outbox, pushes):
    no_quiet(authed)
    subscribe(authed)
    authed.patch("/notifications/settings", json={"kinds": {"deadline_missed": False}})
    impossible(authed)
    run_notify()
    n = next(n for n in authed.get("/notifications").json()["items"] if n["kind"] == "deadline_missed")
    assert n["channels"] == {"push": "off", "email": "off"} and outbox == []


def test_sync_failure_alert_after_30_minutes(authed, outbox):
    me = authed.get("/auth/me").json()
    with session_factory()() as db:
        acct = GoogleAccount(user_id=me["id"], google_sub="s1", email="greg.personal@example.com", status="error",
                             last_error="503 backend error", last_synced_at=now() - timedelta(minutes=10))
        db.add(acct)
        db.commit()
        acct_id = acct.id
    run_notify()
    assert [n for n in authed.get("/notifications").json()["items"] if n["kind"] == "sync_failed"] == []
    run_notify(now() + timedelta(minutes=25))
    alerts = [n for n in authed.get("/notifications").json()["items"] if n["kind"] == "sync_failed"]
    assert len(alerts) == 1 and "greg.personal@example.com" in alerts[0]["title"] and alerts[0]["link"] == "/settings"
    assert len(outbox) == 1
    # Another outage after recovery is a new alert; a second email within 6 hours is throttled.
    with session_factory()() as db:
        acct = db.get(GoogleAccount, acct_id)
        acct.last_synced_at = now() - timedelta(minutes=45)
        db.commit()
    run_notify()
    alerts = [n for n in authed.get("/notifications").json()["items"] if n["kind"] == "sync_failed"]
    assert len(alerts) == 2 and alerts[0]["channels"]["email"] == "throttled"
    # Needs reconnect: alert right away.
    with session_factory()() as db:
        acct = db.get(GoogleAccount, acct_id)
        acct.status, acct.last_synced_at = "needs_reconnect", now()
        db.commit()
    run_notify()
    assert any(n["title"].startswith("Reconnect") for n in authed.get("/notifications").json()["items"])


def test_read_and_isolation(client):
    login(client, "a@example.com")
    with session_factory()() as db:
        a = db.scalars(select(User).where(User.email == "a@example.com")).one()
        notify.create(db, a, "deadline_risk", "Risk A")
        notify.create(db, a, "deadline_risk", "Risk B")
    items = client.get("/notifications").json()
    assert items["unread"] == 2
    nid = items["items"][0]["id"]
    assert client.post(f"/notifications/{nid}/read").status_code == 204
    assert client.get("/notifications").json()["unread"] == 1
    login(client, "b@example.com")
    assert client.get("/notifications").json() == {"items": [], "unread": 0}
    assert client.post(f"/notifications/{nid}/read").status_code == 404
    login(client, "a@example.com")
    assert client.post("/notifications/read-all").status_code == 204
    assert client.get("/notifications?unread_only=true").json()["items"] == []


def test_push_subscriptions(authed, pushes):
    s = authed.get("/notifications/settings").json()
    assert s["push"]["enabled"] is True and s["push"]["subscriptions"] == 0
    subscribe(authed, "https://push.example.com/gone/1")
    subscribe(authed, "https://push.example.com/sub/2")
    subscribe(authed, "https://push.example.com/sub/2")  # same browser again: still one
    assert authed.get("/notifications/settings").json()["push"]["subscriptions"] == 2
    assert authed.post("/push/subscribe", json={"endpoint": "http://insecure", "keys": {"p256dh": "a", "auth": "b"}}).status_code == 422
    no_quiet(authed)
    r = authed.post("/notifications/test")
    assert r.status_code == 200 and r.json()["channels"]["push"] == "sent"
    # The push service said the first subscription is gone (410): it was removed.
    with session_factory()() as db:
        assert [s.endpoint for s in db.scalars(select(PushSubscription))] == ["https://push.example.com/sub/2"]
    assert authed.post("/push/unsubscribe", json={"endpoint": "https://push.example.com/sub/2"}).status_code == 204
    assert authed.get("/notifications/settings").json()["push"]["subscriptions"] == 0


def test_push_not_configured(authed, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "vapid_public_key", "")
    monkeypatch.setattr(get_settings(), "vapid_private_key", "")
    assert authed.get("/notifications/settings").json()["push"]["enabled"] is False
    r = authed.post("/push/subscribe", json={"endpoint": "https://push.example.com/x", "keys": {"p256dh": "a", "auth": "b"}})
    assert r.status_code == 409


def test_settings_validation(authed):
    assert authed.patch("/notifications/settings", json={"quiet_start": "25:00"}).status_code == 422
    assert authed.patch("/notifications/settings", json={"digest_hour": 24}).status_code == 422
    r = authed.patch("/notifications/settings", json={"email_digest": True, "digest_hour": 6, "kinds": {"block_start": True, "bogus": True}})
    p = r.json()["prefs"]
    assert p["email_digest"] is True and p["digest_hour"] == 6 and p["kinds"]["block_start"] is True and "bogus" not in p["kinds"]
    assert p["kinds"]["deadline_missed"] is True  # untouched kinds keep their defaults


def test_daily_digest_once_a_day_and_masked(authed, outbox):
    setup(authed, hours={d: [["00:00", "24:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    task(authed, title="Board memo", duration_min=60, deadline=(now() + timedelta(days=1)).isoformat())
    task(authed, title="Smith custody notes", duration_min=30, confidential=True, deadline=(now() + timedelta(days=2)).isoformat())
    schedule(authed)
    preview = authed.get("/notifications/digest/preview").json()
    assert "Board memo" in preview["text"] and "Smith" not in preview["text"] and "Confidential task" in preview["text"]
    run_notify()
    assert outbox == []  # digest is off by default
    authed.patch("/notifications/settings", json={"email_digest": True, "digest_hour": 0})
    run_notify()
    run_notify()
    digests = [m for m in outbox if m["Subject"].startswith("Your day")]
    assert len(digests) == 1 and "Smith" not in digests[0].get_body(("plain",)).get_content()


def test_block_start_reminder(authed, pushes):
    no_quiet(authed)
    subscribe(authed)
    authed.patch("/notifications/settings", json={"kinds": {"block_start": True}})
    setup(authed, hours={d: [["00:00", "24:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    task(authed, title="Call the insurer", duration_min=30)
    s = schedule(authed)
    start = datetime.fromisoformat(s["blocks"][0]["start"].replace("Z", "+00:00"))
    run_notify(start - timedelta(minutes=3))
    n = [n for n in authed.get("/notifications").json()["items"] if n["kind"] == "block_start"]
    assert len(n) == 1 and n[0]["read_at"] is not None  # a reminder, not something to clear
    assert any("Call the insurer" in p["payload"]["title"] for p in pushes)


def test_worker_tick_runs_notifications(authed):
    from app.worker import tick

    impossible(authed)
    with session_factory()() as db:
        tick(db, None, {}, now())
    assert any(n["kind"] == "deadline_missed" for n in authed.get("/notifications").json()["items"])
