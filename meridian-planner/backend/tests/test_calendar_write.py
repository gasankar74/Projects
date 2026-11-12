"""Writing task blocks to Google Calendar: idempotency (acceptance test 9, write side), read-back, reconciliation."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import session_factory
from app.models import BlockEventLink, User
from app.services import calendar_write
from app.services.google import GoogleClient

from .test_google_sync import ev, oauth
from .test_schedule_api import ALL_DAY, blocks, q, schedule, setup, task, ts

PERSONAL = "greg.personal@example.com"
COURT = "court@group.calendar.google.com"


def now():
    return datetime.now(timezone.utc)


def connect(client, google):
    google.add_account("code-personal", "sub-p", PERSONAL)
    google.add_calendar("sub-p", PERSONAL, "Personal", primary=True)
    google.add_calendar("sub-p", COURT, "Court dates (shared)", role="reader")
    oauth(client, "code-personal")
    cals = {c["summary"]: c for c in client.get("/calendars").json()}
    setup(client, ALL_DAY)
    r = client.patch(f"/calendars/{cals['Personal']['id']}", json={"is_task_target": True})
    assert r.status_code == 200, r.text
    return cals


def ours(google, cal=PERSONAL):
    return [e for e in google.live_events(cal) if ((e.get("extendedProperties") or {}).get("private") or {}).get("meridianApp") == "1"]


def write(client):
    r = client.post("/calendars/write-now")
    assert r.status_code == 200, r.text
    return r.json()


def links():
    with session_factory()() as db:
        return db.scalars(select(BlockEventLink)).all()


def test_blocks_written_to_task_calendar(client, google):
    connect(client, google)
    t = task(client, title="Draft settlement letter", duration_min=60)
    s = schedule(client)
    b = blocks(s, t["id"])[0]
    out = write(client)
    assert out["synced"] == 1 and out["pending"] == 0
    evs = ours(google)
    assert len(evs) == 1
    e = evs[0]
    assert e["summary"] == "Draft settlement letter"
    assert ts(e["start"]["dateTime"]) == ts(b["start"]) and ts(e["end"]["dateTime"]) == ts(b["end"])
    assert e["extendedProperties"]["private"]["meridianTaskId"] == t["id"]
    assert e["id"].startswith("mrdn")
    # Nothing is ever written to the read-only calendar.
    assert not [w for w in google.writes if COURT in w[1]]
    # Our own event comes back through sync but is neither busy time nor shown as a calendar event.
    client.post("/calendars/sync")
    evs_api = client.get(f"/events?start={q(now() - timedelta(days=1))}&end={q(now() + timedelta(days=7))}").json()
    assert not [x for x in evs_api if x["summary"] == "Draft settlement letter"]
    busy = client.get(f"/busy?start={q(now() - timedelta(days=1))}&end={q(now() + timedelta(days=7))}").json()
    assert not any(ts(x["start"]) <= ts(b["start"]) < ts(x["end"]) for x in busy)
    # And the plan doesn't move away from its own event.
    assert blocks(schedule(client), t["id"])[0]["start"] == b["start"]


def test_9_lost_response_retry_creates_no_duplicate(client, google):
    connect(client, google)
    task(client, title="Review discovery", duration_min=60)
    schedule(client)
    google.process_then_fail.append(("POST", 503))  # Google creates the event, but the reply is an error
    out = write(client)
    assert out["pending"] == 1 and out["errors"] == 1
    assert len(ours(google)) == 1
    # Retry later (past the backoff): the insert gets 409 and becomes an update. Still one event.
    with session_factory()() as db:
        user = db.scalar(select(User))
        sent = calendar_write.flush(db, user, GoogleClient(transport=google.transport()), now=now() + timedelta(minutes=5))
    assert sent == 1
    assert len(ours(google)) == 1
    assert [ln.state for ln in links()] == ["synced"]
    # Running the writer again does nothing.
    write(client)
    assert len(ours(google)) == 1


def test_replan_moves_event_and_completion_removes_it(client, google):
    connect(client, google)
    t = task(client, title="Prep deposition", duration_min=60)
    schedule(client)
    write(client)
    eid = ours(google)[0]["id"]
    # A meeting lands on the block: the plan moves it and the event is updated in place (same id).
    b = blocks(schedule(client), t["id"])[0]
    google.put_event(PERSONAL, ev("meeting", ts(b["start"]) - timedelta(minutes=30), ts(b["end"]) + timedelta(minutes=30)))
    client.post("/calendars/sync")
    nb = blocks(schedule(client), t["id"])[0]
    assert nb["start"] != b["start"]
    write(client)
    evs = ours(google)
    assert [e["id"] for e in evs] == [eid] and ts(evs[0]["start"]["dateTime"]) == ts(nb["start"])
    # Completing the task removes its future event.
    client.post(f"/tasks/{t['id']}/complete")
    schedule(client)
    write(client)
    assert ours(google) == []


def test_confidential_task_is_masked(client, google):
    connect(client, google)
    task(client, title="Smith v. Jones privileged strategy", duration_min=30, confidential=True)
    schedule(client)
    write(client)
    e = ours(google)[0]
    assert e["summary"] == "Focus block" and e["visibility"] == "private"
    assert "Smith" not in str(e)


def test_moving_event_in_google_locks_block_there(client, google):
    connect(client, google)
    t = task(client, title="Outline brief", duration_min=60)
    schedule(client)
    write(client)
    e = ours(google)[0]
    new_start = (now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    google.user_moves(PERSONAL, e["id"], new_start, new_start + timedelta(hours=1))
    res = client.post("/calendars/sync").json()["results"][0]
    assert res["calendars"]["Personal"]["moved"] == 1
    b = blocks(schedule(client), t["id"])[0]
    assert ts(b["start"]) == new_start and b["locked"] is True
    assert "Google Calendar" in b["reason"]["why"][0]
    # Nothing is written back (the event is already where the user put it).
    before = len(google.writes)
    write(client)
    assert len(google.writes) == before


def test_event_deleted_in_google_is_recreated(client, google):
    connect(client, google)
    task(client, title="Call client", duration_min=30)
    schedule(client)
    write(client)
    eid = ours(google)[0]["id"]
    google.cancel_event(PERSONAL, eid)
    client.post("/calendars/sync")
    write(client)
    assert [e["id"] for e in ours(google)] == [eid]


def test_reconcile_removes_orphans_and_duplicates_and_restores_missing(client, google):
    connect(client, google)
    task(client, title="File motion", duration_min=30)
    schedule(client)
    write(client)
    real = ours(google)[0]
    start = now() + timedelta(days=1)
    tag = {"private": {"meridianApp": "1", "meridianBlockId": real["extendedProperties"]["private"]["meridianBlockId"]}}
    google.put_event(PERSONAL, ev("mrdnduplicate", start, start + timedelta(minutes=30), extendedProperties=tag))
    google.put_event(PERSONAL, ev("mrdnorphan", start, start + timedelta(minutes=30), extendedProperties={"private": {"meridianApp": "1"}}))
    google.remove_silently(PERSONAL, real["id"])
    with session_factory()() as db:
        user = db.scalar(select(User))
        out = calendar_write.reconcile(db, user, GoogleClient(transport=google.transport()))
    assert out == {"recreated": 1, "orphans_deleted": 2}
    write(client)
    assert [e["id"] for e in ours(google)] == [real["id"]]


def test_turning_off_task_calendar_removes_future_events(client, google):
    cals = connect(client, google)
    task(client, title="Research", duration_min=30)
    schedule(client)
    write(client)
    assert len(ours(google)) == 1
    client.patch(f"/calendars/{cals['Personal']['id']}", json={"is_task_target": False})
    write(client)
    assert ours(google) == []
    assert client.get("/calendars/write-status").json()["calendar"] is None


def test_calendar_that_becomes_read_only_is_never_written(client, google):
    connect(client, google)
    task(client, title="Research", duration_min=30)
    schedule(client)
    write(client)
    google.calendars["sub-p"][0]["accessRole"] = "reader"
    client.post("/calendars/sync")  # the calendar list now says read-only; it stops being the task calendar
    n = len(google.writes)
    task(client, title="Another", duration_min=30)
    schedule(client)
    write(client)
    assert len(google.writes) == n


def test_worker_sends_pending_writes_within_budget(client, google):
    from app.worker import tick

    connect(client, google)
    for i in range(3):
        task(client, title=f"Task {i}", duration_min=30)
    schedule(client)
    t0 = now()
    state = {"synced": t0, "refreshed": t0, "generated": t0, "writes": __import__("collections").deque([t0] * 118)}
    with session_factory()() as db:
        tick(db, GoogleClient(transport=google.transport()), state, now=t0)
    assert len(ours(google)) == 2  # budget of 120 a minute, 118 already used
    with session_factory()() as db:
        tick(db, GoogleClient(transport=google.transport()), state, now=t0 + timedelta(seconds=61))
    assert len(ours(google)) == 3


def test_task_can_choose_its_own_calendar(client, google):
    google.add_account("code-personal", "sub-p", PERSONAL)
    google.add_calendar("sub-p", PERSONAL, "Personal", primary=True)
    google.add_calendar("sub-p", "family@group.calendar.google.com", "Family", role="writer")
    oauth(client, "code-personal")
    cals = {c["summary"]: c for c in client.get("/calendars").json()}
    setup(client, ALL_DAY)
    client.patch(f"/calendars/{cals['Personal']['id']}", json={"is_task_target": True})
    task(client, title="Work thing", duration_min=30)
    task(client, title="Book holiday", duration_min=30, calendar_id=cals["Family"]["id"])
    schedule(client)
    write(client)
    assert [e["summary"] for e in ours(google)] == ["Work thing"]
    assert [e["summary"] for e in ours(google, "family@group.calendar.google.com")] == ["Book holiday"]
