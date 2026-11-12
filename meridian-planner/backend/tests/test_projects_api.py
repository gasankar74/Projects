"""Areas, project detail, Kanban order, templates (save, starters, instantiate) and AI breakdowns."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.ai.providers import ProviderError, ScriptedProvider, set_provider_for_tests

from .test_schedule_api import ALL_DAY, setup, task

NY = ZoneInfo("America/New_York")


def project(c, **kw):
    r = c.post("/projects", json={"name": "Launch", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_areas_group_projects(authed):
    a = authed.post("/areas", json={"name": "Legal practice"}).json()
    p = project(authed, area_id=a["id"])
    assert p["area_id"] == a["id"]
    assert [x["name"] for x in authed.get("/areas").json()] == ["Legal practice"]
    assert authed.patch(f"/areas/{a['id']}", json={"name": "Law"}).json()["name"] == "Law"
    authed.delete(f"/areas/{a['id']}")
    assert authed.get("/projects").json()[0]["area_id"] is None
    # Someone else's area can't be used.
    from .conftest import login

    other = authed.post("/areas", json={"name": "Mine"}).json()
    login(authed, "other@example.com")
    assert authed.post("/projects", json={"name": "X", "area_id": other["id"]}).status_code == 422


def test_project_detail_has_plan_and_progress(authed):
    setup(authed, ALL_DAY)
    p = project(authed)
    a = task(authed, title="Research", duration_min=60, project_id=p["id"])
    b = task(authed, title="Write", duration_min=90, project_id=p["id"])
    authed.post(f"/tasks/{b['id']}/dependencies", json={"depends_on_task_id": a["id"]})
    authed.post(f"/tasks/{a['id']}/complete")
    d = authed.get(f"/projects/{p['id']}").json()
    assert d["project"]["open_count"] == 1 and d["project"]["done_count"] == 1 and d["project"]["open_min"] == 90
    write = next(t for t in d["tasks"] if t["id"] == b["id"])
    assert write["scheduled_start"] and write["depends_on"] == [a["id"]]
    assert d["forecast_finish"]
    assert authed.get("/projects").json()[0]["open_min"] == 90


def test_kanban_reorder_and_status(authed):
    p = project(authed)
    ids = [task(authed, title=f"Card {i}", project_id=p["id"])["id"] for i in range(3)]
    assert authed.post("/tasks/reorder", json={"task_ids": ids[::-1]}).status_code == 204
    titles = [t["title"] for t in authed.get(f"/projects/{p['id']}").json()["tasks"]]
    assert titles == ["Card 2", "Card 1", "Card 0"]
    authed.patch(f"/tasks/{ids[0]}", json={"status": "in_progress"})
    assert authed.get(f"/tasks/{ids[0]}").json()["status"] == "in_progress"
    # Reordering doesn't add history entries.
    assert [e["action"] for e in authed.get(f"/tasks/{ids[1]}/history").json()] == ["created"]


def test_template_round_trip_keeps_offsets_and_dependencies(authed):
    p = project(authed, start_date="2026-12-01")
    d1 = datetime(2026, 12, 3, 17, tzinfo=NY).isoformat()
    d2 = datetime(2026, 12, 10, 17, tzinfo=NY).isoformat()
    a = task(authed, title="Gather documents", duration_min=60, project_id=p["id"], deadline=d1)
    b = task(authed, title="Draft filing", duration_min=120, project_id=p["id"], deadline=d2)
    authed.post(f"/tasks/{b['id']}/dependencies", json={"depends_on_task_id": a["id"]})
    r = authed.post(f"/projects/{p['id']}/save-as-template", json={"name": "Filing"})
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert [i["offset_days"] for i in tpl["items"]] == [2, 9]
    assert tpl["items"][1]["depends_on"] == [0]

    r = authed.post(f"/templates/{tpl['id']}/instantiate", json={"name": "Filing for Lee", "start_date": "2027-01-04"})
    assert r.status_code == 201, r.text
    np = r.json()["project"]
    assert r.json()["tasks_created"] == 2
    d = authed.get(f"/projects/{np['id']}").json()
    by = {t["title"]: t for t in d["tasks"]}
    assert datetime.fromisoformat(by["Draft filing"]["deadline"]).astimezone(NY).date() == date(2027, 1, 13)
    assert by["Draft filing"]["depends_on"] == [by["Gather documents"]["id"]]
    assert datetime.fromisoformat(np["deadline"]).astimezone(NY).date() == date(2027, 1, 13)
    assert authed.post(f"/projects/{p['id']}/save-as-template", json={"name": "Filing"}).status_code == 409


def test_starter_templates(authed):
    out = authed.get("/templates").json()
    assert {s["key"] for s in out["starters"]} >= {"speaking", "mediation", "paper"}
    r = authed.post("/templates/starters/mediation")
    assert r.status_code == 201
    tpl = r.json()
    r = authed.post(f"/templates/{tpl['id']}/instantiate", json={"name": "Lee mediation", "start_date": "2027-02-01"})
    d = authed.get(f"/projects/{r.json()['project']['id']}").json()
    session = next(t for t in d["tasks"] if t["title"] == "Mediation session")
    assert session["is_milestone"] and not session["auto_schedule"]


def test_template_validation(authed):
    bad = {"name": "Bad", "items": [{"title": "A", "depends_on": [1]}, {"title": "B"}]}
    assert authed.post("/templates", json=bad).status_code == 422


def test_ai_breakdown_suggests_then_user_adds(authed):
    p = project(authed, name="Conference talk")
    task(authed, title="Confidential prep", project_id=p["id"], confidential=True)
    fake = ScriptedProvider(structured_answer={"tasks": [
        {"title": "Outline the talk", "duration_min": 100},
        {"title": "Draft slides", "duration_min": 600, "depends_on": [0]},
        {"title": "Rehearse", "duration_min": 45, "depends_on": [1, 7]},
    ]})
    set_provider_for_tests(fake)
    try:
        r = authed.post(f"/projects/{p['id']}/breakdown", json={"goal": "A 30 minute keynote on mediation in March"})
    finally:
        set_provider_for_tests(None)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # Estimates are rounded to 15 minutes and capped; impossible dependencies are dropped.
    assert [i["duration_min"] for i in items] == [105, 480, 45]
    assert items[2]["depends_on"] == [1]
    assert "Confidential prep" not in fake.structured_prompts[0]
    assert "Conference talk" in fake.structured_prompts[0]
    # Nothing saved until the user adds it.
    assert authed.get(f"/projects/{p['id']}").json()["project"]["open_count"] == 1
    r = authed.post(f"/projects/{p['id']}/tasks/bulk", json={"items": items, "from_ai": True})
    assert r.status_code == 201, r.text
    created = r.json()
    assert {t["source"] for t in created} == {"ai"}
    assert created[2]["depends_on"] == [created[1]["id"]]


def test_breakdown_reports_an_unreachable_model(authed):
    p = project(authed)

    class Down(ScriptedProvider):
        def structured(self, *a):
            raise ProviderError("Couldn't reach Ollama")

    set_provider_for_tests(Down())
    try:
        r = authed.post(f"/projects/{p['id']}/breakdown", json={"goal": "something"})
    finally:
        set_provider_for_tests(None)
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]


@pytest.mark.parametrize("access", ["hidden"])
def test_hidden_project_name_not_sent(authed, access):
    p = project(authed, name="Acme acquisition", ai_access=access)
    fake = ScriptedProvider(structured_answer={"tasks": [{"title": "Do it", "duration_min": 30}]})
    set_provider_for_tests(fake)
    try:
        authed.post(f"/projects/{p['id']}/breakdown", json={"goal": "close the deal"})
    finally:
        set_provider_for_tests(None)
    assert "Acme" not in fake.structured_prompts[0]


def test_bulk_rejects_forward_dependencies(authed):
    p = project(authed)
    r = authed.post(f"/projects/{p['id']}/tasks/bulk", json={"items": [{"title": "A", "depends_on": [1]}, {"title": "B"}]})
    assert r.status_code == 422
    assert authed.get(f"/projects/{p['id']}").json()["tasks"] == []
