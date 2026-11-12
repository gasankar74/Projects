"""Motion / CSV import: column guessing, parsing, review decisions, duplicates, re-import."""

from sqlalchemy import select

from app.db import session_factory
from app.models import Task
from app.services.importer import guess_mapping, parse_duration

MOTION = """Name,Description,Status,Priority,Due Date,Duration,Start Date,Project,Workspace,Labels,Assignee,Completed At,ID,Hard Deadline
Draft settlement letter,For the Lopez mediation,Todo,ASAP,11/20/2026,1h 30m,,Lopez mediation,Legal and Mediation,client;draft,Greg,,mot_1,true
Board memo,,In Progress,High,11/18/2026 3:00 PM,45,11/16/2026,,Business Administration,,Greg,,mot_2,false
Renew insurance,,Completed,Medium,11/01/2026,30 min,,,Financial and Insurance,,Greg,11/02/2026,mot_3,
,Row with no name,Todo,Low,,,,,,,,,mot_4,
Prepare keynote slides,,Todo,Medium,11/25/2026,2 hours,,Spring CLE talk,Speaking,,Greg,,mot_5,
Draft settlement letter,duplicate row in the same file,Todo,ASAP,11/20/2026,1h 30m,,Lopez mediation,Legal and Mediation,,Greg,,mot_6,true
"""


def upload(c, text=MOTION, name="motion-export.csv"):
    r = c.post("/imports", json={"filename": name, "text": text})
    assert r.status_code == 201, r.text
    return r.json()


def rows_by_title(job):
    return {r["fields"].get("title") or f"#{r['index']}": r for r in job["rows"]}


def test_parse_duration_formats():
    assert parse_duration("90") == 90
    assert parse_duration("1h 30m") == 90
    assert parse_duration("1.5 hours") == 90
    assert parse_duration("2 hrs") == 120
    assert parse_duration("1:15") == 75
    assert parse_duration("30 minutes") == 30
    assert parse_duration("1 day") == 480
    assert parse_duration("soon") is None


def test_guess_mapping_motion_headers():
    m = guess_mapping(["Name", "Due Date", "Duration", "Workspace", "Labels", "Assignee", "Hard Deadline", "Mystery"])
    assert m == {"Name": "title", "Due Date": "deadline", "Duration": "duration", "Workspace": "category", "Labels": "tags",
                 "Assignee": "notes", "Hard Deadline": "hard_flag", "Mystery": "notes"}


def test_review_classifies_rows(authed):
    job = upload(authed)
    assert job["status"] == "review"
    s = job["summary"]
    assert s["rows"] == 6 and s["errors"] == 1 and s["completed"] == 1 and s["duplicates"] == 1
    assert s["to_import"] == 3
    assert s["new_projects"] == ["Lopez mediation", "Spring CLE talk"]
    assert s["unknown_categories"] == ["Speaking"]  # existing categories match by name; this one doesn't
    rows = rows_by_title(job)
    letter = next(r for r in job["rows"] if r["index"] == 0)
    assert letter["fields"]["priority"] == "urgent" and letter["fields"]["duration_min"] == 90
    assert letter["fields"]["deadline_type"] == "hard"
    # A date with no time means 5 pm in the user's zone (New York): 22:00 UTC in November.
    assert letter["fields"]["deadline"] == "2026-11-20T22:00:00+00:00"
    assert letter["fields"]["tags"] == ["client", "draft"]
    memo = rows["Board memo"]
    assert memo["fields"]["status"] == "in_progress" and memo["fields"]["deadline_type"] == "soft"
    assert memo["fields"]["deadline"] == "2026-11-18T20:00:00+00:00"
    assert rows["Renew insurance"]["state"] == "completed" and rows["Renew insurance"]["action"] == "skip"
    assert next(r for r in job["rows"] if r["index"] == 3)["error"] == "No title"
    dup = next(r for r in job["rows"] if r["index"] == 5)
    assert dup["state"] == "duplicate_in_file" and dup["action"] == "skip"
    assert any("Assignee: Greg" in n for n in letter["notes"])


def test_nothing_saved_until_apply_then_reimport_creates_nothing(authed):
    job = upload(authed)
    with session_factory()() as db:
        assert db.scalars(select(Task)).all() == []
    r = authed.post(f"/imports/{job['id']}/apply")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["created"] == 3 and res["projects_created"] == ["Lopez mediation", "Spring CLE talk"]
    with session_factory()() as db:
        tasks = db.scalars(select(Task).where(Task.source == "import")).all()
        assert {t.external_ref for t in tasks} == {"motion:mot_1", "motion:mot_2", "motion:mot_5"}
        letter = next(t for t in tasks if t.title == "Draft settlement letter")
        assert letter.category_id is not None and letter.project_id is not None and letter.duration_min == 90
        assert "Assignee: Greg" in (letter.notes or "")
    # The raw file is not kept once applied.
    assert authed.get(f"/imports/{job['id']}").json()["status"] == "applied"
    # Same file again: everything is either already imported, an error, or completed.
    again = upload(authed)
    assert again["summary"]["to_import"] == 0
    assert again["summary"]["already_imported"] == 3
    # Applying twice is refused.
    assert authed.post(f"/imports/{job['id']}/apply").status_code == 409


def test_existing_task_is_a_duplicate(authed):
    r = authed.post("/tasks", json={"title": "Prepare keynote slides", "deadline": "2026-11-25T15:00:00-05:00", "duration_min": 60})
    assert r.status_code == 201
    job = upload(authed)
    row = rows_by_title(job)["Prepare keynote slides"]
    assert row["state"] == "duplicate" and row["duplicate_of"]["title"] == "Prepare keynote slides" and row["action"] == "skip"


def test_review_changes_mapping_options_and_decisions(authed):
    job = upload(authed)
    # Include completed tasks; force-import the duplicate row; skip the board memo.
    r = authed.patch(f"/imports/{job['id']}", json={"options": {"include_completed": True}, "decisions": {"5": "import", "1": "skip"}})
    assert r.status_code == 200, r.text
    j = r.json()
    rows = {row["index"]: row for row in j["rows"]}
    assert rows[2]["action"] == "import" and rows[5]["action"] == "import" and rows[1]["action"] == "skip"
    # Re-map the Workspace column to be ignored: no unknown categories any more.
    r = authed.patch(f"/imports/{job['id']}", json={"mapping": {"Workspace": "ignore"}})
    assert r.json()["summary"]["unknown_categories"] == []
    assert authed.patch(f"/imports/{job['id']}", json={"mapping": {"Workspace": "nonsense"}}).status_code == 422
    res = authed.post(f"/imports/{job['id']}/apply").json()
    assert res["created"] == 4
    with session_factory()() as db:
        done = db.scalars(select(Task).where(Task.title == "Renew insurance")).one()
        assert done.status == "done" and done.completed_at is not None and done.remaining_min == 0


def test_cancel_and_isolation(client):
    from .conftest import login

    login(client, "a@example.com")
    job = upload(client)
    login(client, "b@example.com")
    assert client.get(f"/imports/{job['id']}").status_code == 404
    assert client.post(f"/imports/{job['id']}/apply").status_code == 404
    login(client, "a@example.com")
    assert client.post(f"/imports/{job['id']}/cancel").status_code == 204
    assert client.get(f"/imports/{job['id']}").json()["status"] == "cancelled"


def test_bad_files(authed):
    assert authed.post("/imports", json={"filename": "x.csv", "text": ""}).status_code == 422
    assert authed.post("/imports", json={"filename": "x.csv", "text": "Name\n" + "a\n" * 5001}).status_code == 422
    # Semicolon-separated, day-first dates.
    job = upload(authed, "Title;Due;Estimate\nCall the bank;03/12/2026;20\n")
    authed.patch(f"/imports/{job['id']}", json={"options": {"date_order": "dmy"}})
    j = authed.get(f"/imports/{job['id']}").json()
    assert j["rows"][0]["fields"]["deadline"].startswith("2026-12-03")
    assert j["rows"][0]["fields"]["duration_min"] == 20
