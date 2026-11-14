"""Import straight from Motion's API with the user's API key (Motion is mocked; no network)."""

import json

import httpx

from app.main import app
from app.routers.imports import motion_transport

KEY = "test-motion-key"
TASKS = {
    "ws_1": [
        {"id": "mot_1", "name": "Draft settlement letter", "description": "<p>For the <b>Lopez</b> mediation</p>",
         "status": {"name": "Todo", "isResolvedStatus": False}, "completed": False, "priority": "ASAP",
         "dueDate": "2026-11-20T22:00:00.000Z", "deadlineType": "HARD", "duration": 90, "startOn": "2026-11-16",
         "project": {"id": "p1", "name": "Lopez mediation"}, "labels": [{"name": "client"}]},
        {"id": "mot_2", "name": "Call the bank", "status": {"name": "Todo"}, "priority": "LOW", "dueDate": "2026-11-21T22:00:00.000Z",
         "deadlineType": "NONE", "duration": "REMINDER"},
    ],
    "ws_2": [
        {"id": "mot_3", "name": "Renew insurance", "status": {"name": "Completed", "isResolvedStatus": True}, "completed": True,
         "priority": "MEDIUM", "dueDate": "2026-11-01T21:00:00.000Z", "deadlineType": "SOFT", "duration": "NONE",
         "completedTime": "2026-11-02T15:00:00.000Z"},
    ],
}


def motion(calls, limited=0, bad_key=False):
    state = {"limited": limited}

    def handler(req: httpx.Request):
        calls.append(req)
        if bad_key or req.headers.get("X-API-Key") != KEY:
            return httpx.Response(401, json={"message": "Unauthorized"})
        if state["limited"]:
            state["limited"] -= 1
            return httpx.Response(429, headers={"retry-after": "1"})
        if req.url.path == "/v1/workspaces":
            return httpx.Response(200, json={"workspaces": [{"id": "ws_1", "name": "Legal"}, {"id": "ws_2", "name": "Admin"}], "meta": {}})
        ws = req.url.params.get("workspaceId")
        items = TASKS[ws]
        if req.url.params.get("includeAllStatuses") != "true":
            items = [t for t in items if not t.get("completed")]
        # Two pages for the first workspace, to cover the cursor.
        if ws == "ws_1" and not req.url.params.get("cursor"):
            return httpx.Response(200, json={"tasks": items[:1], "meta": {"nextCursor": "c2"}})
        if ws == "ws_1":
            return httpx.Response(200, json={"tasks": items[1:], "meta": {}})
        return httpx.Response(200, json={"tasks": items, "meta": {}})

    return httpx.MockTransport(handler)


def use(transport, monkeypatch):
    app.dependency_overrides[motion_transport] = lambda: transport
    monkeypatch.setattr("app.services.motion_api.time.sleep", lambda s: None)


def test_import_from_motion_api(authed, monkeypatch):
    calls = []
    use(motion(calls), monkeypatch)
    try:
        r = authed.post("/imports/motion", json={"api_key": KEY})
        assert r.status_code == 201, r.text
        job = r.json()
        assert job["filename"] == "Motion (2 tasks)"
        rows = {r["fields"]["title"]: r for r in job["rows"]}
        letter = rows["Draft settlement letter"]["fields"]
        assert letter["priority"] == "urgent" and letter["deadline_type"] == "hard" and letter["duration_min"] == 90
        assert letter["deadline"] == "2026-11-20T22:00:00+00:00"
        assert letter["description"] == "For the Lopez mediation"
        assert letter["project"] == "Lopez mediation" and letter["category"] == "Legal" and letter["tags"] == ["client"]
        bank = rows["Call the bank"]
        assert bank["fields"]["deadline"] is None and bank["fields"]["duration_min"] == 5  # a reminder, with no deadline
        assert any("no deadline" in n for n in bank["notes"])
        # The key went only to Motion, in the header; it's not in the saved import.
        assert all(c.headers["X-API-Key"] == KEY and c.url.host == "api.usemotion.com" for c in calls)
        assert KEY not in json.dumps(job)

        assert authed.post(f"/imports/{job['id']}/apply").json()["created"] == 2

        # Fetching again finds the same Motion ids and imports nothing new.
        again = authed.post("/imports/motion", json={"api_key": KEY}).json()
        assert again["summary"]["to_import"] == 0 and again["summary"]["already_imported"] == 2
    finally:
        app.dependency_overrides.pop(motion_transport, None)


def test_motion_include_completed_and_rate_limit(authed, monkeypatch):
    calls = []
    use(motion(calls, limited=2), monkeypatch)
    try:
        job = authed.post("/imports/motion", json={"api_key": KEY, "include_completed": True}).json()
        rows = {r["fields"]["title"]: r for r in job["rows"]}
        ins = rows["Renew insurance"]
        assert ins["fields"]["status"] == "done" and ins["action"] == "import"
        assert ins["fields"]["completed_at"] == "2026-11-02T15:00:00+00:00"
        assert sum(1 for c in calls if c.url.path == "/v1/workspaces") == 3  # two 429s, then the answer
    finally:
        app.dependency_overrides.pop(motion_transport, None)


def test_motion_bad_key(authed, monkeypatch):
    use(motion([], bad_key=True), monkeypatch)
    try:
        r = authed.post("/imports/motion", json={"api_key": "wrong"})
        assert r.status_code == 422 and "didn't accept that API key" in r.json()["detail"]
        assert "wrong" not in r.text
    finally:
        app.dependency_overrides.pop(motion_transport, None)
