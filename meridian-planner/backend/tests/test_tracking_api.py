"""Time tracking, duration learning, the daily Top 3, the weekly dashboard, workload and reports."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import update

from app.db import session_factory
from app.models import TimeEntry

from .test_schedule_api import ALL_DAY, blocks, schedule, setup, task

NY = ZoneInfo("America/New_York")


def now():
    return datetime.now(timezone.utc)


def backdate_running(minutes: int):
    with session_factory()() as db:
        db.execute(update(TimeEntry).where(TimeEntry.ended_at.is_(None)).values(started_at=now() - timedelta(minutes=minutes)))
        db.commit()


def test_timer_logs_work_against_the_estimate(authed):
    t = task(authed, title="Draft brief", duration_min=120)
    r = authed.post("/time/start", json={"task_id": t["id"]})
    assert r.status_code == 200, r.text
    assert authed.get(f"/tasks/{t['id']}").json()["status"] == "in_progress"
    backdate_running(45)
    cur = authed.get("/time/current").json()
    assert cur["task_id"] == t["id"] and cur["minutes"] == 45 and cur["running"]
    out = authed.post("/time/stop", json={}).json()
    assert out["minutes"] == 45 and not out["running"]
    assert authed.get(f"/tasks/{t['id']}").json()["remaining_min"] == 75
    assert authed.get("/time/current").json() is None


def test_starting_another_timer_stops_the_first(authed):
    a = task(authed, title="A", duration_min=60)
    b = task(authed, title="B", duration_min=60)
    authed.post("/time/start", json={"task_id": a["id"]})
    backdate_running(20)
    authed.post("/time/start", json={"task_id": b["id"]})
    entries = authed.get("/time/entries").json()
    assert {e["title"]: e["running"] for e in entries} == {"A": False, "B": True}
    assert authed.get(f"/tasks/{a['id']}").json()["remaining_min"] == 40


def test_stop_and_complete(authed):
    t = task(authed, title="Quick", duration_min=30)
    authed.post("/time/start", json={"task_id": t["id"]})
    authed.post("/time/stop", json={"complete": True})
    assert authed.get(f"/tasks/{t['id']}").json()["status"] == "done"
    assert authed.post("/time/start", json={"task_id": t["id"]}).status_code == 422


def test_manual_entries(authed):
    t = task(authed, title="Research", duration_min=60)
    s = now() - timedelta(hours=3)
    r = authed.post("/time/entries", json={"task_id": t["id"], "started_at": s.isoformat(), "minutes": 50, "reduce_remaining": True})
    assert r.status_code == 201, r.text
    assert authed.get(f"/tasks/{t['id']}").json()["remaining_min"] == 10
    e = r.json()
    assert authed.patch(f"/time/entries/{e['id']}", json={"ended_at": (s - timedelta(minutes=5)).isoformat()}).status_code == 422
    assert authed.delete(f"/time/entries/{e['id']}").status_code == 204
    bad = {"task_id": t["id"], "started_at": s.isoformat()}
    assert authed.post("/time/entries", json=bad).status_code == 422


def _finished(c, title, est, actual, cat=None):
    t = task(c, title=title, duration_min=est, category_id=cat)
    c.post("/time/entries", json={"task_id": t["id"], "started_at": (now() - timedelta(days=1)).isoformat(), "minutes": actual})
    c.post(f"/tasks/{t['id']}/complete")
    return t


def test_duration_learning(authed):
    setup(authed, ALL_DAY)
    cat = authed.post("/categories", json={"name": "Writing"}).json()["id"]
    for i in range(4):
        _finished(authed, f"Essay {i}", 60, 90, cat)
    est = authed.get("/reports/estimates").json()
    row = next(r for r in est["categories"] if r["category"] == "Writing")
    assert row["samples"] == 4 and row["median_ratio"] == 1.5 and row["factor"] is None  # not enough history yet
    _finished(authed, "Essay 4", 60, 90, cat)
    row = next(r for r in authed.get("/reports/estimates").json()["categories"] if r["category"] == "Writing")
    assert row["factor"] == 1.5

    t = task(authed, title="Essay 5", duration_min=60, category_id=cat)
    blk = blocks(schedule(authed), t["id"])
    assert sum((datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).seconds // 60 for b in blk) == 60
    # Opt in: the scheduler now plans 1.5x for this category. The task itself keeps the user's estimate.
    authed.patch("/schedule/settings", json={"learn_durations": True})
    blk = blocks(schedule(authed), t["id"])
    assert sum((datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).seconds // 60 for b in blk) == 90
    assert authed.get(f"/tasks/{t['id']}").json()["duration_min"] == 60
    assert authed.get("/schedule/settings").json()["learn_durations"] is True


def test_top3_suggested_then_picked(authed):
    setup(authed, ALL_DAY)
    low = task(authed, title="Low", duration_min=30, priority="low")
    urgent = task(authed, title="Urgent", duration_min=30, priority="urgent", deadline=(now() + timedelta(hours=8)).isoformat())
    mid = task(authed, title="Mid", duration_min=30, priority="medium")
    extra = task(authed, title="Extra", duration_min=30, priority="high")
    schedule(authed)
    out = authed.get("/focus/top3").json()
    assert out["source"] == "suggested" and len(out["items"]) <= 3
    assert out["items"][0]["title"] == "Urgent"
    day = datetime.now(NY).date().isoformat()
    r = authed.put("/focus/top3", json={"day": day, "task_ids": [low["id"], mid["id"]]})
    assert r.json()["source"] == "you" and [i["title"] for i in r.json()["items"]] == ["Low", "Mid"]
    assert [i["title"] for i in authed.get(f"/schedule/agenda?day={day}").json()["top3"]] == ["Low", "Mid"]
    assert authed.put("/focus/top3", json={"day": day, "task_ids": [low["id"], mid["id"], urgent["id"], extra["id"]]}).status_code == 422
    assert authed.put("/focus/top3", json={"day": day, "task_ids": []}).json()["source"] == "suggested"


def test_week_dashboard_and_workload(authed):
    setup(authed, ALL_DAY)
    cat = authed.post("/categories", json={"name": "Legal", "color": "#b91c1c"}).json()["id"]
    task(authed, title="Brief", duration_min=120, category_id=cat, deadline=(now() + timedelta(days=2)).isoformat())
    _finished(authed, "Filed motion", 60, 75, cat)
    schedule(authed)
    w = authed.get("/dashboard/week").json()
    assert len(w["days"]) == 7
    assert w["totals"]["done"] >= 0 and w["totals"]["planned_min"] >= 0
    assert all(k in w for k in ("deadlines", "risks", "tracked_by_category", "estimate_vs_actual"))
    wl = authed.get("/workload?days=7").json()
    assert len(wl["days"]) == 7
    legal = next(g for g in wl["groups"] if g["label"] == "Legal")
    assert legal["minutes"] == 120 and legal["color"] == "#b91c1c"
    assert sum(d["by_group"].get(legal["key"], 0) for d in wl["days"]) == 120
    assert authed.get("/workload?days=7&group=project").json()["groups"][0]["label"] == "No project"


def test_reports_summary_and_csv(authed):
    cat = authed.post("/categories", json={"name": "Speaking"}).json()["id"]
    _finished(authed, "Keynote prep", 60, 80, cat)
    today = datetime.now(NY).date()
    start = (today - timedelta(days=7)).isoformat()
    end = today.isoformat()
    s = authed.get(f"/reports/summary?start={start}&end={end}").json()
    assert s["tracked_total_min"] == 80
    assert s["tracked_by_category"][0]["label"] == "Speaking"
    assert s["completed"] == 1 and s["estimates"][0]["ratio"] == round(80 / 60, 2)
    assert sum(d["minutes"] for d in s["tracked_by_day"]) == 80
    r = authed.get(f"/reports/time.csv?start={start}&end={end}")
    assert r.status_code == 200 and "Keynote prep" in r.text and r.text.startswith("date,start,end,minutes")
    assert authed.get(f"/reports/summary?start={end}&end={start}").status_code == 422


def test_tracking_is_private(client):
    from .conftest import login

    login(client, "a@example.com")
    t = task(client, title="Mine")
    e = client.post("/time/entries", json={"task_id": t["id"], "started_at": (now() - timedelta(hours=1)).isoformat(), "minutes": 30}).json()
    login(client, "b@example.com")
    assert client.post("/time/start", json={"task_id": t["id"]}).status_code == 404
    assert client.delete(f"/time/entries/{e['id']}").status_code == 404
    assert client.get("/time/entries").json() == []
