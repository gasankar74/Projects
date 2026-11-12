"""Scheduling profiles, travel time and the new scheduling settings, through the API."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .test_google_sync import ev, oauth
from .test_schedule_api import ALL_DAY, blocks, q, schedule, setup, task, ts

NY = ZoneInfo("America/New_York")
EVENINGS = {d: [["18:00", "21:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}


def profile(c, **kw):
    r = c.post("/profiles", json={"name": "Personal", "work_hours": EVENINGS, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_task_profile_uses_its_hours(authed):
    setup(authed, {d: [["09:00", "17:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    p = profile(authed)
    t = task(authed, title="Call plumber", scheduling_profile_id=p["id"])
    b = blocks(schedule(authed), t["id"])[0]
    loc = ts(b["start"]).astimezone(NY)
    assert 18 <= loc.hour < 21
    assert any("Personal" in w for w in b["reason"]["why"])


def test_category_profile_applies_and_falls_back_when_deleted(authed):
    setup(authed, {d: [["09:00", "17:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
    p = profile(authed, daily_cap_min=60)
    cat = authed.post("/categories", json={"name": "Family", "scheduling_profile_id": p["id"]}).json()
    assert cat["scheduling_profile_id"] == p["id"]
    ts_ = [task(authed, title=f"Errand {i}", category_id=cat["id"]) for i in range(2)]
    s = schedule(authed)
    days = {ts(blocks(s, t["id"])[0]["start"]).astimezone(NY).date() for t in ts_}
    assert len(days) == 2  # 60-minute daily cap for the profile
    assert authed.delete(f"/profiles/{p['id']}").status_code == 204
    fam = next(c for c in authed.get("/categories").json() if c["id"] == cat["id"])
    assert fam["scheduling_profile_id"] is None
    s = authed.post("/schedule/replan", json={}).json()
    for t in ts_:
        assert 9 <= ts(blocks(s, t["id"])[0]["start"]).astimezone(NY).hour < 17


def test_profile_validation_and_privacy(authed, client):
    assert authed.post("/profiles", json={"name": "Bad", "work_hours": {"funday": [["09:00", "10:00"]]}}).status_code == 422
    assert authed.post("/profiles", json={"name": "Bad", "work_hours": {"mon": [["10:00", "09:00"]]}}).status_code == 422
    p = profile(authed)
    assert authed.post("/profiles", json={"name": "Personal"}).status_code == 409
    r = authed.patch(f"/profiles/{p['id']}", json={"daily_cap_min": 90, "work_hours": {"sat": [["08:00", "12:00"], ["13:00", "15:00"]]}})
    assert r.status_code == 200 and r.json()["daily_cap_min"] == 90
    authed.post("/auth/logout")
    from .conftest import login

    login(client, email="other@example.com")
    assert client.get("/profiles").json() == []
    assert client.post("/tasks", json={"title": "x", "scheduling_profile_id": p["id"]}).status_code == 422
    assert client.post("/categories", json={"name": "x", "scheduling_profile_id": p["id"]}).status_code == 422


def test_new_settings_round_trip_and_validation(authed):
    r = authed.patch("/schedule/settings", json={
        "energy_hours": {"high": ["08:00", "11:00"], "low": ["14:00", "16:00"]},
        "optimize_hours": 24, "holidays": ["2026-12-25", "2026-12-24", "2026-12-25"], "write_days": 7, "event_travel_min": 20,
    })
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["energy_hours"]["high"] == ["08:00", "11:00"] and s["optimize_hours"] == 24
    assert s["holidays"] == ["2026-12-24", "2026-12-25"] and s["write_days"] == 7 and s["event_travel_min"] == 20
    assert authed.patch("/schedule/settings", json={"energy_hours": {"medium": ["08:00", "11:00"]}}).status_code == 422
    assert authed.patch("/schedule/settings", json={"optimize_hours": 500}).status_code == 422
    assert authed.patch("/schedule/settings", json={"energy_hours": None}).json()["energy_hours"] == {}


def test_holiday_has_no_default_work_hours(authed):
    tomorrow = (datetime.now(timezone.utc).astimezone(NY) + timedelta(days=1)).date()
    setup(authed, {d: [["09:00", "17:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
          holidays=[(tomorrow + timedelta(days=i)).isoformat() for i in range(-1, 2)])
    t = task(authed, title="Anything", duration_min=60)
    b = blocks(schedule(authed), t["id"])[0]
    assert ts(b["start"]).astimezone(NY).date() >= tomorrow + timedelta(days=2)


def test_travel_time_around_located_events_and_tasks(client, google):
    google.add_account("code-personal", "sub-p", "greg.personal@example.com")
    google.add_calendar("sub-p", "greg.personal@example.com", "Personal", primary=True)
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    google.put_event("greg.personal@example.com", ev("court", start, start + timedelta(hours=1), location="Superior Court, 111 N Hill St"))
    google.put_event("greg.personal@example.com", ev("zoom", start + timedelta(hours=3), start + timedelta(hours=4), location="https://zoom.us/j/123"))
    oauth(client, "code-personal")
    setup(client, ALL_DAY, event_travel_min=30)
    busy = client.get(f"/busy?start={q(start - timedelta(hours=2))}&end={q(start + timedelta(hours=6))}").json()
    # /busy shows raw calendar time; the scheduler adds travel only for the physical location.
    assert len(busy) == 2
    t = task(client, title="Site visit", duration_min=60, travel_min=15, earliest_start=(start + timedelta(hours=1)).isoformat())
    b = blocks(schedule(client), t["id"])[0]
    # 30 min travel after the court event + 15 min travel before the visit.
    assert ts(b["start"]) >= start + timedelta(hours=1, minutes=45)
