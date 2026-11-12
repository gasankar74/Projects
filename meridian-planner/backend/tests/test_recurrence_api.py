"""Repeating tasks through the API: acceptance tests 4 and 11, missed policies, edits, idempotency."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import session_factory
from app.models import RecurringSeries, Task, User
from app.scheduler.recurrence import Rule
from app.services import recurrence as rec

NY = ZoneInfo("America/New_York")
WEEKLY_FRI = {"kind": "fixed", "rrule": "FREQ=WEEKLY;BYDAY=FR"}
LAST_BIZ = {"kind": "fixed", "rrule": "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", "adjust": "previous"}


def today():
    return datetime.now(timezone.utc).astimezone(NY).date()


def occurrences(c, series_id):
    items = c.get(f"/tasks?view=all&series_id={series_id}").json()["items"]
    return sorted([t for t in items if t["series_id"] == series_id], key=lambda t: t["occurrence_due"])


def create(c, **kw):
    r = c.post("/tasks", json={"title": "Weekly report", "duration_min": 60, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_fixed_series_generated_ahead_and_once(authed):
    t = create(authed, recurrence=WEEKLY_FRI)
    assert t["series_id"] and t["occurrence_due"]
    occ = occurrences(authed, t["series_id"])
    dues = [date.fromisoformat(o["occurrence_due"]) for o in occ]
    assert all(d.weekday() == 4 for d in dues)
    assert dues == sorted(set(dues)) and len(dues) >= 2
    assert all(d <= today() + timedelta(days=14) for d in dues[1:])
    # Deadline at the due time (17:00 local), and work may start in the week before.
    first = occ[0]
    dl = datetime.fromisoformat(first["deadline"]).astimezone(NY)
    assert (dl.hour, dl.minute) == (17, 0) and dl.date() == dues[0]
    es = datetime.fromisoformat(first["earliest_start"]).astimezone(NY)
    assert es.date() == dues[0] - timedelta(days=6)
    # Running generation again creates nothing new (idempotent).
    with session_factory()() as db:
        s = db.get(RecurringSeries, t["series_id"])
        u = db.get(User, s.user_id)
        assert rec.generate(db, u, s) == []
        db.commit()
    assert len(occurrences(authed, t["series_id"])) == len(occ)


def test_4_completing_generates_next_occurrence_completion_based(authed):
    t = create(authed, title="Water plants", recurrence={"kind": "after_completion", "every": 2, "unit": "weeks"})
    assert len(occurrences(authed, t["series_id"])) == 1
    r = authed.post(f"/tasks/{t['id']}/complete")
    assert r.status_code == 200
    occ = occurrences(authed, t["series_id"])
    assert len(occ) == 2
    nxt = occ[-1]
    assert nxt["status"] == "todo" and nxt["title"] == "Water plants" and nxt["remaining_min"] == 60
    first_due = date.fromisoformat(t["occurrence_due"])
    assert first_due == today() + timedelta(weeks=2)  # no date given: due one interval out
    # Done today, so the next one is two weeks from today, but never on or before the current one's date.
    assert date.fromisoformat(nxt["occurrence_due"]) == max(today() + timedelta(weeks=2), first_due + timedelta(days=1))
    # Completing again (idempotent) doesn't add another.
    authed.post(f"/tasks/{t['id']}/complete")
    assert len(occurrences(authed, t["series_id"])) == 2


def test_4_completing_last_open_fixed_occurrence_generates_next(authed):
    t = create(authed, title="Month-end billing", recurrence=LAST_BIZ)
    occ = occurrences(authed, t["series_id"])
    assert len(occ) == 1  # next month-end is the only one (beyond the 14-day window, but one is always kept open)
    authed.post(f"/tasks/{occ[0]['id']}/complete")
    occ = occurrences(authed, t["series_id"])
    assert len(occ) == 2
    r = Rule.from_json(LAST_BIZ)
    assert date.fromisoformat(occ[1]["occurrence_due"]) == r.next_fixed(today(), date.fromisoformat(occ[0]["occurrence_due"]))
    # Completed one goes to history; the new one is open.
    assert occ[0]["status"] == "done" and occ[1]["status"] == "todo"


def test_11_last_business_day_preview(authed):
    r = authed.post("/series/preview", json={"rule": LAST_BIZ, "start": "2026-11-01", "count": 4})
    assert r.status_code == 200, r.text
    assert r.json()["dates"] == ["2026-11-30", "2026-12-31", "2027-01-29", "2027-02-26"]
    # With Dec 31 as a holiday in settings, the occurrence moves to Dec 30.
    authed.patch("/schedule/settings", json={"holidays": ["2026-12-31"]})
    r = authed.post("/series/preview", json={"rule": LAST_BIZ, "start": "2026-12-01", "count": 1})
    assert r.json()["dates"] == ["2026-12-30"]


def test_11_last_business_day_series_dates(authed):
    t = create(authed, title="Trust account reconciliation", recurrence=LAST_BIZ)
    due = date.fromisoformat(t["occurrence_due"])
    nxt_month = (due.replace(day=28) + timedelta(days=4)).replace(day=1)
    assert due.weekday() < 5 and due.month != nxt_month.month
    # No business day after it in the same month.
    d = due + timedelta(days=1)
    while d.month == due.month:
        assert d.weekday() >= 5
        d += timedelta(days=1)


def test_quick_add_creates_series(authed):
    r = authed.post("/tasks/quick-add", json={"text": "Pay rent on the last business day of every month 15m"})
    assert r.status_code in (200, 201), r.text
    task = r.json()["task"]
    assert task["series_id"]
    assert task["recurrence"]["adjust"] == "previous"
    s = authed.get(f"/series/{task['series_id']}").json()
    assert s["active"] and len(s["upcoming"]) >= 1


def test_missed_policy_skip_and_latest_only(authed):
    t = create(authed, title="Daily log", duration_min=15, recurrence={"kind": "fixed", "rrule": "FREQ=DAILY"})
    sid = t["series_id"]
    later = datetime.now(timezone.utc) + timedelta(days=3)
    with session_factory()() as db:
        s = db.get(RecurringSeries, sid)
        u = db.get(User, s.user_id)
        s.missed_policy = "skip"
        rec.generate(db, u, s, now=later)
        db.commit()
        tasks = db.scalars(select(Task).where(Task.series_id == sid)).all()
        missed = [x for x in tasks if x.deadline < later]
        assert missed and all(x.status == "cancelled" for x in missed)
        assert all(x.status == "todo" for x in tasks if x.deadline >= later)

    t2 = create(authed, title="Inbox zero", duration_min=15, recurrence={"kind": "fixed", "rrule": "FREQ=DAILY", "missed": "latest_only"})
    with session_factory()() as db:
        s = db.get(RecurringSeries, t2["series_id"])
        assert s.missed_policy == "latest_only"
        u = db.get(User, s.user_id)
        far = datetime.now(timezone.utc) + timedelta(days=30)  # everything generated so far is missed
        rec.generate(db, u, s, now=far)
        db.commit()
        tasks = db.scalars(select(Task).where(Task.series_id == s.id)).all()
        open_ = [x for x in tasks if x.status == "todo"]
        assert len(open_) >= 1
        newest = max(x.occurrence_due for x in tasks)
        assert all(x.occurrence_due == newest or x.deadline >= far for x in open_)


def test_deleted_occurrence_is_not_regenerated(authed):
    t = create(authed, recurrence=WEEKLY_FRI)
    occ = occurrences(authed, t["series_id"])
    victim = occ[-1]
    assert authed.delete(f"/tasks/{victim['id']}").status_code == 204
    with session_factory()() as db:
        s = db.get(RecurringSeries, t["series_id"])
        rec.generate(db, db.get(User, s.user_id), s)
        db.commit()
    dues = [o["occurrence_due"] for o in occurrences(authed, t["series_id"]) if o["id"] != victim["id"]]
    assert victim["occurrence_due"] not in dues


def test_editing_series_updates_unstarted_future_occurrences(authed):
    t = create(authed, recurrence=WEEKLY_FRI)
    sid = t["series_id"]
    occ = occurrences(authed, sid)
    started = occ[-1]
    authed.patch(f"/tasks/{started['id']}", json={"completion_pct": 50})
    r = authed.patch(f"/series/{sid}", json={"title": "Weekly client report", "template": {"duration_min": 90, "priority": "high"}})
    assert r.status_code == 200, r.text
    after = {o["id"]: o for o in occurrences(authed, sid)}
    for oid, o in after.items():
        if oid == started["id"]:
            assert o["title"] == "Weekly report"  # in progress: left alone
        else:
            assert o["title"] == "Weekly client report" and o["duration_min"] == 90 and o["priority"] == "high"


def test_changing_rule_regenerates_future(authed):
    t = create(authed, recurrence=WEEKLY_FRI)
    sid = t["series_id"]
    r = authed.patch(f"/series/{sid}", json={"rule": {"kind": "fixed", "rrule": "FREQ=WEEKLY;BYDAY=MO"}})
    assert r.status_code == 200, r.text
    open_ = [o for o in occurrences(authed, sid) if o["status"] == "todo"]
    assert open_ and all(date.fromisoformat(o["occurrence_due"]).weekday() == 0 for o in open_)


def test_stop_and_delete_series(authed):
    t = create(authed, recurrence=WEEKLY_FRI)
    sid = t["series_id"]
    assert authed.patch(f"/series/{sid}", json={"active": False}).json()["active"] is False
    n = len(occurrences(authed, sid))
    with session_factory()() as db:
        s = db.get(RecurringSeries, sid)
        assert rec.generate(db, db.get(User, s.user_id), s, now=datetime.now(timezone.utc) + timedelta(days=60)) == []
    assert authed.delete(f"/series/{sid}?delete_open=true").status_code == 204
    assert authed.get(f"/series/{sid}").status_code == 404
    remaining = [x for x in authed.get(f"/tasks?view=all&series_id={sid}").json()["items"] if x["series_id"] == sid]
    assert not [x for x in remaining if x["status"] == "todo"] and n >= 1


def test_bad_rule_rejected(authed):
    r = authed.post("/tasks", json={"title": "X", "recurrence": {"kind": "fixed", "rrule": "FREQ=SOMETIMES"}})
    assert r.status_code == 422


def test_series_are_private(authed, client):
    t = create(authed, recurrence=WEEKLY_FRI)
    authed.post("/auth/logout")
    from .conftest import login

    login(client, email="other@example.com")
    assert client.get(f"/series/{t['series_id']}").status_code == 404
    assert client.get("/series").json() == []
