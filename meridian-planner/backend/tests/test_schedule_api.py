"""End-to-end scheduler tests through the HTTP API, Google Calendar fake included."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import session_factory
from app.models import User
from app.worker import tick

from .test_google_sync import ev, oauth, setup_two_accounts

ALL_DAY = {d: [["00:00", "24:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
NINE_FIVE = {d: [["09:00", "17:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}


def now():
    return datetime.now(timezone.utc)


def q(dt: datetime) -> str:
    return dt.isoformat().replace("+", "%2B")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def setup(c, hours=ALL_DAY, **extra):
    # freeze_min=0: the tests run on the real clock, so don't let "about to start" freezing depend on the minute.
    r = c.patch("/schedule/settings", json={"work_hours": hours, "freeze_min": 0, **extra})
    assert r.status_code == 200, r.text


def task(c, **kw):
    body = {"title": "Task", "duration_min": 60, "category_id": None, **kw}
    r = c.post("/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def schedule(c):
    r = c.get("/schedule")
    assert r.status_code == 200, r.text
    return r.json()


def blocks(s, tid):
    return sorted([b for b in s["blocks"] if b["task_id"] == tid], key=lambda b: b["start"])


def kinds(s):
    return {w["kind"] for w in s["warnings"]}


# -- acceptance tests ------------------------------------------------------------------------------------------


def test_1_two_hour_task_before_deadline(authed):
    setup(authed)
    t = task(authed, title="Write brief", duration_min=120, deadline=(now() + timedelta(days=3)).isoformat())
    s = schedule(authed)
    bl = blocks(s, t["id"])
    assert sum((ts(b["end"]) - ts(b["start"])).seconds // 60 for b in bl) == 120
    assert ts(bl[-1]["end"]) <= ts(t["deadline"])
    got = authed.get(f"/tasks/{t['id']}").json()
    assert got["scheduled_start"] and got["schedule_status"] == "scheduled"
    why = authed.get(f"/schedule/tasks/{t['id']}/why").json()
    assert why["blocks"] and why["stats"]["parts"]["P"] == 0.4


def test_2_new_meeting_moves_task_not_meeting(client, google):
    setup_two_accounts(google)
    oauth(client, "code-personal")
    setup(client)
    t = task(client, title="Write brief", duration_min=60, deadline=(now() + timedelta(days=2)).isoformat())
    first = blocks(schedule(client), t["id"])[0]
    # A meeting lands exactly on the task's slot (more than 30 min out, so it's not frozen).
    start = max(ts(first["start"]), now() + timedelta(hours=1))
    start = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    # Move the task there first so the meeting really conflicts with it.
    client.patch(f"/schedule/blocks/{first['id']}", json={"start": start.isoformat()})
    blk = blocks(schedule(client), t["id"])[0]
    client.patch(f"/schedule/blocks/{blk['id']}", json={"locked": False})
    google.put_event("greg.personal@example.com", ev("partner-meeting", start, start + timedelta(hours=1), summary="Partner meeting"))
    assert client.post("/calendars/sync").status_code == 200
    s = schedule(client)
    moved = blocks(s, t["id"])[0]
    assert not (ts(moved["start"]) < start + timedelta(hours=1) and start < ts(moved["end"]))
    assert "busy on your calendar" in moved["reason"]["why"][0]
    events = client.get(f"/events?start={q(start - timedelta(hours=1))}&end={q(start + timedelta(hours=2))}").json()
    assert [(e["summary"], ts(e["start_at"])) for e in events] == [("Partner meeting", start)]


def test_3_urgent_task_moves_lower_priority_work(authed):
    setup(authed)
    low = task(authed, title="Tidy files", duration_min=120, priority="low")
    first = blocks(schedule(authed), low["id"])[0]
    urgent = task(authed, title="Emergency motion", duration_min=120, priority="urgent", deadline=(now() + timedelta(hours=8)).isoformat())
    s = schedule(authed)
    u, lo = blocks(s, urgent["id"])[0], blocks(s, low["id"])[0]
    assert u["start"] == first["start"]
    assert ts(lo["start"]) >= ts(u["end"])


def test_5_impossible_hard_deadline_alerts(authed):
    setup(authed, hours=NINE_FIVE)
    t = task(authed, title="Appeal brief", duration_min=600, deadline=(now() + timedelta(hours=3)).isoformat())
    s = schedule(authed)
    assert "cannot_meet_deadline" in kinds(s)
    assert all(ts(b["end"]) <= ts(t["deadline"]) for b in blocks(s, t["id"]))
    assert authed.get(f"/tasks/{t['id']}").json()["at_risk"] is True


def test_6_dependency_order(authed):
    setup(authed)
    pre = task(authed, title="Gather exhibits", duration_min=90, priority="low")
    dep = task(authed, title="Draft motion", duration_min=60, priority="urgent")
    assert authed.post(f"/tasks/{dep['id']}/dependencies", json={"depends_on_task_id": pre["id"]}).status_code in (200, 201, 204)
    s = schedule(authed)
    assert ts(blocks(s, dep["id"])[0]["start"]) >= ts(blocks(s, pre["id"])[-1]["end"])


def test_7_partial_completion_only_remaining(authed):
    setup(authed)
    t = task(authed, title="Research memo", duration_min=120)
    schedule(authed)
    authed.patch(f"/tasks/{t['id']}", json={"completion_pct": 50})
    s = schedule(authed)
    assert sum((ts(b["end"]) - ts(b["start"])).seconds // 60 for b in blocks(s, t["id"])) == 60


def test_8_overlapping_events_across_accounts(client, google):
    setup_two_accounts(google)
    base = (now() + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    google.put_event("greg.personal@example.com", ev("a", base, base + timedelta(minutes=90)))
    google.put_event("family@group.calendar.google.com", ev("b", base + timedelta(hours=1), base + timedelta(hours=2)))
    google.put_event("greg@lawfirm.example.com", ev("c", base + timedelta(hours=2), base + timedelta(hours=3)))
    oauth(client, "code-personal")
    oauth(client, "code-biz", mode="connect")
    setup(client)
    t = task(client, title="Prep", duration_min=60, earliest_start=base.isoformat())
    bl = blocks(schedule(client), t["id"])[0]
    assert ts(bl["start"]) == base + timedelta(hours=3)


def test_10_locked_block_survives_replan(authed):
    setup(authed)
    t = task(authed, title="Client prep", duration_min=60)
    b = blocks(schedule(authed), t["id"])[0]
    later = (ts(b["start"]) + timedelta(days=1)).replace(minute=0)
    s = authed.patch(f"/schedule/blocks/{b['id']}", json={"start": later.isoformat()}).json()
    moved = blocks(s, t["id"])[0]
    assert ts(moved["start"]) == later and moved["locked"]
    s = authed.post("/schedule/replan", json={"fresh": True}).json()
    kept = blocks(s, t["id"])[0]
    assert ts(kept["start"]) == later and kept["locked"]
    # Unlock: the next fresh replan is free to move it back to the earliest slot.
    authed.patch(f"/schedule/blocks/{kept['id']}", json={"locked": False})
    s = authed.post("/schedule/replan", json={"fresh": True}).json()
    assert ts(blocks(s, t["id"])[0]["start"]) < later


def test_12_overload_recommends_deferral(authed):
    setup(authed, hours=NINE_FIVE)
    deadline = (now() + timedelta(days=2)).isoformat()
    task(authed, title="Trial prep", duration_min=600, priority="urgent", deadline=deadline, can_split=True)
    task(authed, title="Board report", duration_min=600, priority="high", deadline=deadline, can_split=True)
    w = task(authed, title="Website copy", duration_min=600, priority="low", deadline=deadline, can_split=True)
    s = schedule(authed)
    assert "cannot_meet_deadline" in kinds(s)
    assert s["overload"]["feasible_after_defer"]
    assert w["id"] in [d["task_id"] for d in s["overload"]["defer"]]


def test_13_time_zone_change_replans_in_new_zone(authed):
    setup(authed, hours=NINE_FIVE)
    t = task(authed, title="Prep", duration_min=60)
    schedule(authed)
    r = authed.patch("/auth/me", json={"timezone": "Asia/Tokyo"})
    assert r.status_code == 200
    s = schedule(authed)
    assert s["run"]["trigger"].startswith("time zone changed")
    start = ts(blocks(s, t["id"])[0]["start"]).astimezone(ZoneInfo("Asia/Tokyo"))
    assert 9 <= start.hour < 17


def test_14_completed_task_leaves_the_plan(authed):
    setup(authed)
    t = task(authed, title="Call clerk", duration_min=15)
    assert blocks(schedule(authed), t["id"])
    authed.post(f"/tasks/{t['id']}/complete")
    assert not blocks(schedule(authed), t["id"])


# -- runs, undo, what-if, triggers -----------------------------------------------------------------------------


def test_unchanged_input_skips_run_and_undo_restores(authed):
    setup(authed)
    a = task(authed, title="A", duration_min=60)
    s1 = schedule(authed)
    s2 = authed.get("/schedule").json()
    assert s1["run"]["id"] == s2["run"]["id"]  # nothing changed: no new run
    urgent = task(authed, title="B", duration_min=60, priority="urgent")
    s3 = schedule(authed)
    assert s3["run"]["id"] != s1["run"]["id"] and s3["can_undo"]
    assert blocks(s3, urgent["id"])[0]["start"] == blocks(s1, a["id"])[0]["start"]
    s4 = authed.post("/schedule/undo").json()
    assert blocks(s4, a["id"])[0]["start"] == blocks(s1, a["id"])[0]["start"]
    assert s4["run"]["trigger"].startswith("undo")
    runs = authed.get("/schedule/runs").json()
    assert len(runs) >= 3


def test_what_if_does_not_save(authed):
    setup(authed, hours=NINE_FIVE)
    a = task(authed, title="A", duration_min=60)
    before = schedule(authed)
    r = authed.post("/schedule/what-if", json={"add_tasks": [{"title": "Big new thing", "duration_min": 120, "priority": "urgent"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(c["task_id"] == a["id"] for c in body["changes"])
    assert any(c["title"] == "Big new thing" for c in body["changes"])
    after = authed.get("/schedule").json()
    assert after["run"]["id"] == before["run"]["id"]


def test_worker_debounces_and_replans(authed):
    setup(authed)
    schedule(authed)
    t = task(authed, title="New", duration_min=30)
    with session_factory()() as db:
        user = db.query(User).one()
        assert user.schedule_dirty_at is not None
        tick(db, None, {"refreshed": now()})  # too soon: debounced
        db.refresh(user)
        assert user.schedule_dirty_at is not None
        user.schedule_dirty_at = now() - timedelta(seconds=10)
        db.commit()
        tick(db, None, {"refreshed": now()})
        db.refresh(user)
        assert user.schedule_dirty_at is None
    s = authed.get("/schedule?refresh=false").json()
    assert blocks(s, t["id"])


def test_agenda_and_eisenhower(client, google):
    setup_two_accounts(google)
    soon = (now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    google.put_event("greg.personal@example.com", ev("standup", soon, soon + timedelta(minutes=30), summary="Standup"))
    oauth(client, "code-personal")
    setup(client)
    u = task(client, title="Urgent filing", duration_min=30, priority="urgent", deadline=(now() + timedelta(hours=20)).isoformat())
    task(client, title="Someday", duration_min=30, priority="low")
    day = soon.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    ag = client.get(f"/schedule/agenda?day={day}").json()
    kinds_ = {i["kind"] for i in ag["items"]}
    assert "event" in kinds_
    box = client.get("/schedule/eisenhower").json()
    assert [x["id"] for x in box["do"]] == [u["id"]]
    assert [x["title"] for x in box["drop"]] == ["Someday"]


def test_settings_validation_and_isolation(client):
    from .conftest import login

    login(client, "a@example.com")
    assert client.patch("/schedule/settings", json={"work_hours": {"funday": [["09:00", "17:00"]]}}).status_code == 422
    assert client.patch("/schedule/settings", json={"work_hours": {"mon": [["17:00", "09:00"]]}}).status_code == 422
    assert client.patch("/schedule/settings", json={"weights": {"Z": 1}}).status_code == 422
    ok = client.patch("/schedule/settings", json={"daily_cap_min": 300, "transition_min": 10}).json()
    assert ok["daily_cap_min"] == 300 and ok["transition_min"] == 10
    a = task(client, title="Mine", duration_min=30)
    b_id = blocks(schedule(client), a["id"])[0]["id"]
    login(client, "b@example.com")
    assert client.patch(f"/schedule/blocks/{b_id}", json={"locked": True}).status_code == 404
    assert client.get(f"/schedule/tasks/{a['id']}/why").status_code == 404
    assert schedule(client)["blocks"] == []


def test_change_made_during_a_replan_is_not_lost(authed):
    """A task saved while a replan is running (after its snapshot) must leave the user marked for another replan."""
    from app.services import scheduling

    setup(authed)
    task(authed, title="First")
    with session_factory()() as db:
        user = db.query(User).one()
        seen = user.schedule_dirty_at  # what the replan saw when it took its snapshot
        snap = scheduling.build_snapshot(db, user)
        # Meanwhile another request saves a task and marks the user dirty again (committed).
        with session_factory()() as other:
            u2 = other.get(User, user.id)
            scheduling.mark_dirty(u2, "task created: Second")
            other.commit()
        scheduling.save_plan(db, user, scheduling.plan(snap), "test", seen_dirty=seen)
        db.refresh(user)
        assert user.schedule_dirty_at is not None and user.schedule_dirty_reason == "task created: Second"
