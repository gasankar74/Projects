from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from app.db import session_factory
from app.models import CalendarEvent, GoogleAccount

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def iso(dt):
    return dt.isoformat()


def oauth(client, code, mode="signin"):
    r = client.get(f"/auth/google/login?mode={mode}&tz=America/New_York", follow_redirects=False)
    assert r.status_code == 307, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    assert "https://www.googleapis.com/auth/calendar.events" in q["scope"][0]
    r = client.get(f"/auth/google/callback?code={code}&state={q['state'][0]}", follow_redirects=False)
    assert r.status_code == 307, r.text
    return r


def setup_two_accounts(google):
    google.add_account("code-personal", "sub-p", "greg.personal@example.com")
    google.add_calendar("sub-p", "greg.personal@example.com", "Personal", primary=True)
    google.add_calendar("sub-p", "family@group.calendar.google.com", "Family", role="writer")
    google.add_calendar("sub-p", "court@group.calendar.google.com", "Court dates (shared)", role="reader")
    google.add_account("code-biz", "sub-b", "greg@lawfirm.example.com")
    google.add_calendar("sub-b", "greg@lawfirm.example.com", "Law firm", primary=True)
    google.add_calendar("sub-b", "colleague@lawfirm.example.com", "Colleague", role="freeBusyReader")


def ev(id, start, end, **kw):
    return {"id": id, "summary": kw.pop("summary", id), "start": {"dateTime": iso(start)}, "end": {"dateTime": iso(end)}, **kw}


def test_signin_connects_and_syncs(client, google):
    setup_two_accounts(google)
    google.put_event("greg.personal@example.com", ev("dentist", NOW + timedelta(hours=2), NOW + timedelta(hours=3)))
    oauth(client, "code-personal")
    me = client.get("/auth/me").json()
    assert me["email"] == "greg.personal@example.com" and me["timezone"] == "America/New_York"
    cals = {c["summary"]: c for c in client.get("/calendars").json()}
    assert set(cals) == {"Personal", "Family", "Court dates (shared)"}
    assert cals["Court dates (shared)"]["writable"] is False
    events = client.get(f"/events?start={iso(NOW)}&end={iso(NOW + timedelta(days=1))}".replace("+", "%2B")).json()
    assert [e["summary"] for e in events] == ["dentist"]
    # Tokens are stored encrypted, never in plain text.
    with session_factory()() as db:
        acct = db.scalar(select(GoogleAccount))
        assert acct.refresh_token_enc and "rt:" not in acct.refresh_token_enc


def test_connect_second_account(client, google):
    setup_two_accounts(google)
    oauth(client, "code-personal")
    oauth(client, "code-biz", mode="connect")
    accts = client.get("/google-accounts").json()
    assert {a["email"] for a in accts} == {"greg.personal@example.com", "greg@lawfirm.example.com"}
    # Still the same user, now with five calendars.
    assert client.get("/auth/me").json()["email"] == "greg.personal@example.com"
    assert len(client.get("/calendars").json()) == 5


def test_overlapping_events_across_calendars_merge_into_busy_time(client, google):
    """Acceptance test 8 (data side): overlapping events across calendars and accounts are one busy span."""
    setup_two_accounts(google)
    t = NOW + timedelta(days=1)
    google.put_event("greg.personal@example.com", ev("a", t, t + timedelta(hours=1)))
    google.put_event("family@group.calendar.google.com", ev("b", t + timedelta(minutes=30), t + timedelta(hours=2)))
    google.put_event("greg@lawfirm.example.com", ev("c", t + timedelta(hours=1, minutes=30), t + timedelta(hours=3)))
    google.put_event("colleague@lawfirm.example.com", ev("d", t + timedelta(hours=5), t + timedelta(hours=6)))
    # Free and declined events never block time.
    google.put_event("greg.personal@example.com", ev("free", t + timedelta(hours=8), t + timedelta(hours=9), transparency="transparent"))
    google.put_event("greg@lawfirm.example.com", ev("declined", t + timedelta(hours=10), t + timedelta(hours=11), attendees=[{"email": "x", "self": True, "responseStatus": "declined"}]))
    oauth(client, "code-personal")
    oauth(client, "code-biz", mode="connect")
    busy = client.get("/busy", params={"start": iso(t - timedelta(hours=1)), "end": iso(t + timedelta(hours=12))}).json()
    spans = [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy]
    assert spans == [(t, t + timedelta(hours=3)), (t + timedelta(hours=5), t + timedelta(hours=6))]
    # Turning off "blocks time" on a calendar removes its events from busy time.
    colleague = [c for c in client.get("/calendars").json() if c["summary"] == "Colleague"][0]
    client.patch(f"/calendars/{colleague['id']}", json={"blocks_time": False})
    busy = client.get("/busy", params={"start": iso(t - timedelta(hours=1)), "end": iso(t + timedelta(hours=12))}).json()
    assert len(busy) == 1


def test_incremental_sync_handles_changes_and_cancellations(client, google):
    setup_two_accounts(google)
    cal = "greg.personal@example.com"
    google.put_event(cal, ev("m1", NOW + timedelta(hours=1), NOW + timedelta(hours=2), summary="Mediation"))
    google.put_event(cal, ev("m2", NOW + timedelta(hours=3), NOW + timedelta(hours=4), summary="Call"))
    oauth(client, "code-personal")
    google.put_event(cal, ev("m1", NOW + timedelta(hours=5), NOW + timedelta(hours=6), summary="Mediation (moved)"))
    google.cancel_event(cal, "m2")
    google.put_event(cal, ev("m3", NOW + timedelta(hours=7), NOW + timedelta(hours=8), summary="New"))
    res = client.post("/calendars/sync").json()["results"][0]
    assert res["ok"] and res["calendars"]["Personal"] == {"created": 1, "updated": 1, "cancelled": 1, "removed": 0, "full": False}
    events = client.get("/events", params={"start": iso(NOW), "end": iso(NOW + timedelta(days=1))}).json()
    assert [e["summary"] for e in events] == ["Mediation (moved)", "New"]


def test_sync_failure_recovers_without_duplicates(client, google):
    """Acceptance test 9 (read side): failed and repeated syncs never duplicate events."""
    setup_two_accounts(google)
    cal = "greg.personal@example.com"
    google.put_event(cal, ev("e1", NOW + timedelta(hours=1), NOW + timedelta(hours=2)))
    oauth(client, "code-personal")
    google.put_event(cal, ev("e2", NOW + timedelta(hours=3), NOW + timedelta(hours=4)))
    google.fail_next.append(("/calendars/greg.personal@example.com/events", 503))
    res = client.post("/calendars/sync").json()["results"][0]
    assert res["ok"] is False
    acct = client.get("/google-accounts").json()[0]
    assert acct["status"] == "error"
    cal_row = [c for c in client.get("/calendars").json() if c["summary"] == "Personal"][0]
    assert "injected failure" in cal_row["last_error"]
    # Retry succeeds; then an expired sync token forces a full re-sync. Still exactly one row per event.
    assert client.post("/calendars/sync").json()["results"][0]["ok"] is True
    google.expire_sync_tokens = cal
    google.remove_silently(cal, "e1")
    res = client.post("/calendars/sync").json()["results"][0]
    assert res["calendars"]["Personal"]["full"] is True and res["calendars"]["Personal"]["removed"] == 1
    client.post("/calendars/sync")
    with session_factory()() as db:
        ids = db.scalars(select(CalendarEvent.google_event_id)).all()
        assert sorted(ids) == ["e2"]
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 1
    assert client.get("/google-accounts").json()[0]["status"] == "ok"


def test_revoked_token_flags_reconnect_and_keeps_events(client, google):
    setup_two_accounts(google)
    google.put_event("greg.personal@example.com", ev("keep", NOW + timedelta(hours=1), NOW + timedelta(hours=2)))
    oauth(client, "code-personal")
    with session_factory()() as db:
        acct = db.scalar(select(GoogleAccount))
        acct.token_expiry = NOW - timedelta(hours=1)  # force a refresh
        db.commit()
    google.revoked.update(google.refresh_tokens)
    res = client.post("/calendars/sync").json()["results"][0]
    assert res["ok"] is False
    acct = client.get("/google-accounts").json()[0]
    assert acct["status"] == "needs_reconnect"
    events = client.get("/events", params={"start": iso(NOW), "end": iso(NOW + timedelta(days=1))}).json()
    assert [e["summary"] for e in events] == ["keep"]


def test_read_only_calendar_cannot_be_task_target(client, google):
    setup_two_accounts(google)
    oauth(client, "code-personal")
    cals = {c["summary"]: c for c in client.get("/calendars").json()}
    r = client.patch(f"/calendars/{cals['Court dates (shared)']['id']}", json={"is_task_target": True})
    assert r.status_code == 422
    assert client.patch(f"/calendars/{cals['Family']['id']}", json={"is_task_target": True}).json()["is_task_target"]
    assert client.patch(f"/calendars/{cals['Personal']['id']}", json={"is_task_target": True}).json()["is_task_target"]
    targets = [c["summary"] for c in client.get("/calendars").json() if c["is_task_target"]]
    assert targets == ["Personal"]
    # A task cannot be assigned to a read-only calendar either.
    r = client.post("/tasks", json={"title": "x", "calendar_id": cals["Court dates (shared)"]["id"]})
    assert r.status_code == 422


def test_all_day_and_time_zones(client, google):
    """All-day events and events in other zones are stored as correct UTC instants (DST-safe)."""
    setup_two_accounts(google)
    cal = "greg.personal@example.com"
    google.put_event(cal, {"id": "conf", "summary": "Conference", "start": {"date": "2027-03-14"}, "end": {"date": "2027-03-15"}, "transparency": "opaque"})
    google.put_event(cal, {"id": "holiday", "summary": "Holiday", "start": {"date": "2027-03-15"}, "end": {"date": "2027-03-16"}, "transparency": "transparent"})
    # 9:00 in London on the day US clocks have changed but UK clocks have not.
    google.put_event(cal, {"id": "london", "summary": "London call", "start": {"dateTime": "2027-03-15T09:00:00", "timeZone": "Europe/London"}, "end": {"dateTime": "2027-03-15T10:00:00", "timeZone": "Europe/London"}})
    oauth(client, "code-personal")
    events = {e["summary"]: e for e in client.get("/events", params={"start": "2027-03-13T00:00:00Z", "end": "2027-03-17T00:00:00Z"}).json()}
    assert events["Conference"]["all_day"] and events["Conference"]["start_date"] == "2027-03-14"
    # Midnight in New York on the morning clocks change (still EST, UTC-5).
    assert datetime.fromisoformat(events["Conference"]["start_at"]) == datetime(2027, 3, 14, 5, 0, tzinfo=timezone.utc)
    # The next midnight is after the change (EDT, UTC-4).
    assert datetime.fromisoformat(events["Holiday"]["start_at"]) == datetime(2027, 3, 15, 4, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(events["London call"]["start_at"]) == datetime(2027, 3, 15, 9, 0, tzinfo=timezone.utc)
    # An all-day event left as "free" (Google's default) does not block time; one marked busy does.
    assert events["Holiday"]["is_busy"] is False
    assert events["Conference"]["is_busy"] is True


def test_disconnect_removes_calendars_but_not_tasks(client, google):
    setup_two_accounts(google)
    oauth(client, "code-personal")
    client.post("/tasks", json={"title": "keep me"})
    acct = client.get("/google-accounts").json()[0]
    assert client.delete(f"/google-accounts/{acct['id']}").status_code == 204
    assert client.get("/calendars").json() == []
    assert client.get("/tasks").json()["total"] == 1


def test_oauth_state_is_checked(client, google):
    setup_two_accounts(google)
    client.get("/auth/google/login", follow_redirects=False)
    r = client.get("/auth/google/callback?code=code-personal&state=forged.sig", follow_redirects=False)
    assert r.status_code == 400
