from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

from .conftest import login


def cats(c):
    return {x["name"]: x["id"] for x in c.get("/categories").json()}


def test_requires_login(client):
    assert client.get("/tasks").status_code == 401


def test_new_user_gets_default_categories(authed):
    names = list(cats(authed))
    assert names[:3] == ["Legal and Mediation", "Speaking and Presentations", "Business Administration"]
    assert len(names) == 8


def test_create_task_with_all_fields(authed):
    c = cats(authed)
    proj = authed.post("/projects", json={"name": "Vicarious Trauma CLE", "category_id": c["Speaking and Presentations"]}).json()
    body = {
        "title": "Create slides",
        "description": "40 slides",
        "priority": "high",
        "category_id": c["Speaking and Presentations"],
        "project_id": proj["id"],
        "deadline": "2026-12-01T17:00:00-05:00",
        "deadline_type": "hard",
        "preferred_date": "2026-11-27",
        "earliest_start": "2026-11-16T09:00:00-05:00",
        "duration_min": 240,
        "can_split": True,
        "min_session_min": 60,
        "max_session_min": 120,
        "preferred_time_of_day": "morning",
        "allowed_days": [0, 1, 2, 3, 4],
        "windows": [{"days": [0, 2], "start": "08:00", "end": "12:00"}],
        "buffer_before_min": 10,
        "energy": "high",
        "tags": ["cle", "slides"],
        "links": [{"title": "Outline", "url": "https://example.com/outline"}],
        "checklist": ["Title slide", "Objectives"],
        "client": "State Bar",
        "confidential": False,
    }
    r = authed.post("/tasks", json=body)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["status"] == "todo"  # has a home, so not in the inbox
    assert t["remaining_min"] == 240
    assert [i["text"] for i in t["checklist"]] == ["Title slide", "Objectives"]
    assert t["links"][0]["url"] == "https://example.com/outline"
    assert t["windows"][0]["start"] == "08:00"


def test_validation(authed):
    assert authed.post("/tasks", json={"title": "x", "duration_min": 30, "min_session_min": 60}).status_code == 422
    assert authed.post("/tasks", json={"title": "x", "min_session_min": 60, "max_session_min": 30, "duration_min": 120}).status_code == 422
    assert authed.post("/tasks", json={"title": "x", "allowed_days": [7]}).status_code == 422
    assert authed.post("/tasks", json={"title": ""}).status_code == 422
    assert authed.post("/tasks", json={"title": "x", "priority": "critical"}).status_code == 422


def test_inbox_and_triage(authed):
    t = authed.post("/tasks", json={"title": "Loose idea"}).json()
    assert t["status"] == "inbox"
    assert [x["id"] for x in authed.get("/tasks?view=inbox").json()["items"]] == [t["id"]]
    # Moving to a category triages it out of the inbox without creating a new task.
    c = cats(authed)
    t2 = authed.patch(f"/tasks/{t['id']}", json={"category_id": c["Personal"]}).json()
    assert t2["id"] == t["id"] and t2["status"] == "todo"
    assert authed.get("/tasks?view=inbox").json()["total"] == 0
    # Moving between categories keeps one task (no duplicates).
    authed.patch(f"/tasks/{t['id']}", json={"category_id": c["Education"]})
    assert authed.get("/tasks?view=all").json()["total"] == 1


def test_complete_moves_to_history(authed):
    """Acceptance test 14: completed tasks leave active views but remain in history."""
    t = authed.post("/tasks", json={"title": "File brief", "duration_min": 60}).json()
    done = authed.post(f"/tasks/{t['id']}/complete").json()
    assert done["status"] == "done" and done["completed_at"] and done["remaining_min"] == 0
    assert authed.get("/tasks").json()["total"] == 0
    hist = authed.get("/tasks?view=history").json()["items"]
    assert [x["id"] for x in hist] == [t["id"]]
    assert authed.get(f"/tasks/{t['id']}").status_code == 200
    events = [e["action"] for e in authed.get(f"/tasks/{t['id']}/history").json()]
    assert events == ["completed", "created"]
    reopened = authed.post(f"/tasks/{t['id']}/reopen").json()
    assert reopened["status"] == "todo" and reopened["remaining_min"] == 60


def test_partial_progress_tracks_remaining(authed):
    """Data side of acceptance test 7: only remaining duration is left to schedule."""
    t = authed.post("/tasks", json={"title": "Draft chapter", "duration_min": 240}).json()
    t = authed.patch(f"/tasks/{t['id']}", json={"remaining_min": 90}).json()
    assert t["remaining_min"] == 90 and t["completion_pct"] == 62 and t["status"] == "in_progress"
    t = authed.patch(f"/tasks/{t['id']}", json={"completion_pct": 50}).json()
    assert t["remaining_min"] == 120
    # Re-estimate keeps the share done.
    t = authed.patch(f"/tasks/{t['id']}", json={"duration_min": 300}).json()
    assert t["remaining_min"] == 150


def test_dependencies_and_cycles(authed):
    a = authed.post("/tasks", json={"title": "Research"}).json()
    b = authed.post("/tasks", json={"title": "Outline"}).json()
    c = authed.post("/tasks", json={"title": "Slides"}).json()
    assert authed.post(f"/tasks/{b['id']}/dependencies", json={"depends_on_task_id": a["id"]}).status_code == 200
    r = authed.post(f"/tasks/{c['id']}/dependencies", json={"depends_on_task_id": b["id"]}).json()
    assert r["depends_on"] == [b["id"]]
    cyc = authed.post(f"/tasks/{a['id']}/dependencies", json={"depends_on_task_id": c["id"]})
    assert cyc.status_code == 422 and "loop" in cyc.json()["detail"]
    assert authed.post(f"/tasks/{a['id']}/dependencies", json={"depends_on_task_id": a["id"]}).status_code == 422
    assert authed.get(f"/tasks/{a['id']}").json()["blocking"] == [b["id"]]


def test_filters_and_overdue(authed):
    c = cats(authed)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    authed.post("/tasks", json={"title": "Late filing", "deadline": past, "priority": "low", "category_id": c["Legal and Mediation"]})
    authed.post("/tasks", json={"title": "Urgent call", "deadline": soon, "priority": "urgent", "duration_min": 15})
    authed.post("/tasks", json={"title": "Read paper", "priority": "medium", "duration_min": 120, "tags": ["Research"]})
    items = authed.get("/tasks").json()["items"]
    assert items[0]["title"] == "Late filing" and items[0]["overdue"] is True  # overdue first
    assert items[1]["title"] == "Urgent call"
    assert [t["title"] for t in authed.get("/tasks?view=overdue").json()["items"]] == ["Late filing"]
    assert authed.get(f"/tasks?category_id={c['Legal and Mediation']}").json()["total"] == 1
    assert authed.get("/tasks?priority=urgent&priority=low").json()["total"] == 2
    assert {t["title"] for t in authed.get("/tasks?max_duration=20").json()["items"]} == {"Urgent call"}
    assert authed.get("/tasks?tag=research").json()["total"] == 1
    assert authed.get("/tasks?q=paper").json()["total"] == 1


def test_quick_add_endpoint(authed):
    r = authed.post("/tasks/quick-add", json={"text": "Prepare CLE proposal by Friday, high priority, 3 hours.", "preview": True}).json()
    assert r["task"] is None and r["parsed"]["duration_min"] == 180
    assert authed.get("/tasks?view=all").json()["total"] == 0  # preview saves nothing
    r = authed.post("/tasks/quick-add", json={"text": "Review QuickBooks every Friday for 30 minutes"}).json()
    t = r["task"]
    assert t["title"] == "Review QuickBooks" and t["status"] == "inbox" and t["source"] == "quick_add"
    assert t["recurrence"]["rrule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert t["category_id"] == cats(authed)["Financial and Insurance"]


def test_users_are_isolated(client):
    login(client, "a@example.com")
    t = client.post("/tasks", json={"title": "A's private task"}).json()
    with TestClient(app) as other:
        login(other, "b@example.com")
        assert other.get(f"/tasks/{t['id']}").status_code == 404
        assert other.patch(f"/tasks/{t['id']}", json={"title": "hijack"}).status_code == 404
        assert other.get("/tasks?view=all").json()["total"] == 0
        a_cat = client.get("/categories").json()[0]["id"]
        assert other.post("/tasks", json={"title": "x", "category_id": a_cat}).status_code == 422


def test_soft_delete(authed):
    t = authed.post("/tasks", json={"title": "Temp"}).json()
    assert authed.delete(f"/tasks/{t['id']}").status_code == 204
    assert authed.get(f"/tasks/{t['id']}").status_code == 404
    assert authed.get("/tasks?view=all").json()["total"] == 0


def test_export_import_round_trip_without_duplicates(authed):
    c = cats(authed)
    authed.post("/tasks", json={"title": "One", "category_id": c["Education"], "checklist": ["a"]})
    authed.post("/tasks", json={"title": "Two", "deadline": "2026-12-01T17:00:00Z"})
    exported = authed.get("/export/json")
    assert exported.status_code == 200
    data = exported.json()
    assert len(data["tasks"]) == 2
    # Importing into the same account skips everything (same ids).
    r = authed.post("/import/json", content=exported.content).json()
    assert r == {"created": 0, "skipped_duplicates": 2}
    csv_text = authed.get("/export/tasks.csv").text
    assert csv_text.splitlines()[0].startswith("id,title,status")
    assert len(csv_text.strip().splitlines()) == 3
    # Into a fresh account, everything comes across once.
    with TestClient(app) as other:
        login(other, "new@example.com")
        r = other.post("/import/json", content=exported.content).json()
        assert r == {"created": 2, "skipped_duplicates": 0}
        r = other.post("/import/json", content=exported.content).json()
        assert r["created"] == 0
        one = [t for t in other.get("/tasks?view=all").json()["items"] if t["title"] == "One"][0]
        assert one["category_id"] == cats(other)["Education"]
        assert one["checklist"][0]["text"] == "a"


def test_account_deletion(authed):
    authed.post("/tasks", json={"title": "x"})
    assert authed.post("/account/delete", json={"confirm_email": "wrong@example.com"}).status_code == 422
    assert authed.post("/account/delete", json={"confirm_email": "greg@example.com"}).status_code == 204
    assert authed.get("/auth/me").status_code == 401
    login(authed)
    assert authed.get("/tasks?view=all").json()["total"] == 0


def test_timezone_setting(authed):
    assert authed.patch("/auth/me", json={"timezone": "Not/AZone"}).status_code == 422
    assert authed.patch("/auth/me", json={"timezone": "Europe/London"}).json()["timezone"] == "Europe/London"
