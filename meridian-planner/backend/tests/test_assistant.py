"""The AI assistant: tool calls persist through the service layer, large changes wait for confirmation, confidential
tasks stay hidden, and the reply never claims an unsaved change. Acceptance test 15 is the first test.

The model is replaced by a scripted provider that behaves like one: it reads the tool results it is given and decides
the next call from them."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.ai.providers import ProviderError, Reply, ScriptedProvider, ToolCall, set_provider_for_tests
from app.db import session_factory
from app.models import AiAction, Task, TaskEvent

from .test_calendar_write import connect, ours, write
from .test_schedule_api import ALL_DAY, blocks, schedule, setup, task, ts

NY = ZoneInfo("America/New_York")


@pytest.fixture
def model():
    p = ScriptedProvider()
    set_provider_for_tests(p)
    yield p
    set_provider_for_tests(None)


def call(name, **args):
    return Reply(tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=args)])


def last_tool(msgs) -> dict:
    return json.loads([m for m in msgs if m["role"] == "tool"][-1]["content"])


def conv(c):
    r = c.post("/assistant/conversations")
    assert r.status_code == 201, r.text
    return r.json()["id"]


def say(c, cid, text):
    r = c.post(f"/assistant/conversations/{cid}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def db_task(tid) -> Task:
    with session_factory()() as db:
        return db.get(Task, tid)


def local_day(offset: int):
    return (datetime.now(NY) + timedelta(days=offset)).date()


# -- acceptance test 15 -------------------------------------------------------------------------------------------


def test_15_assistant_reschedule_is_persisted_and_synced(client, google, model):
    """The user asks the assistant to move a task to another day. After they confirm, the task is changed in the
    database, the plan puts it on that day, and the block in Google Calendar moves there too."""
    connect(client, google)
    t = task(client, title="Board memo", duration_min=60)
    first = blocks(schedule(client), t["id"])[0]
    write(client)
    assert ts(ours(google)[0]["start"]["dateTime"]) == ts(first["start"])
    target = local_day(3)

    model.steps = [
        call("find_tasks", query="board memo"),
        lambda msgs: call("reschedule_task", task=last_tool(msgs)["tasks"][0]["ref"], day=target.isoformat()),
        lambda msgs: Reply(content=f"That moves the board memo to {target:%A}. Press Confirm to apply it.") if last_tool(msgs).get("needs_confirmation") else Reply(content="?"),
    ]
    cid = conv(client)
    out = say(client, cid, "Move the board memo to later this week")

    # Moving work to another day waits for the user: nothing has changed yet.
    pending = [a for a in out["actions"] if a["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["preview"]["items"][0]["title"] == "Board memo"
    move = [m for m in pending[0]["preview"]["moves"] if m["task_id"] == t["id"]][0]
    assert datetime.fromisoformat(move["to"]).astimezone(NY).date() == target
    assert db_task(t["id"]).preferred_date is None
    assert blocks(schedule(client), t["id"])[0]["start"] == first["start"]
    final = out["messages"][-1]
    assert final["role"] == "assistant" and "Confirm" in final["content"] and final["changes"] == []

    r = client.post(f"/assistant/actions/{pending[0]['id']}/confirm")
    assert r.status_code == 200, r.text
    note = r.json()["message"]
    assert note["role"] == "note" and note["changes"][0]["task_id"] == t["id"]

    # Persisted: the task row, its history (actor ai), and the plan.
    saved = db_task(t["id"])
    assert saved.preferred_date == target
    with session_factory()() as db:
        ev = db.scalars(select(TaskEvent).where(TaskEvent.task_id == saved.id, TaskEvent.actor == "ai")).all()
        assert ev and "preferred_date" in ev[-1].diff
    b = blocks(schedule(client), t["id"])[0]
    assert ts(b["start"]).astimezone(NY).date() == target
    # Synchronised: the Google event follows the block, and there is still exactly one.
    write(client)
    evs = ours(google)
    assert len(evs) == 1
    assert ts(evs[0]["start"]["dateTime"]) == ts(b["start"])
    # Confirming twice does nothing.
    assert client.post(f"/assistant/actions/{pending[0]['id']}/confirm").status_code == 409


def test_small_change_is_applied_and_reported_from_the_database(authed, model):
    setup(authed, ALL_DAY)
    t = task(authed, title="Call the venue", duration_min=30, priority="low")
    model.steps = [
        call("find_tasks", query="venue"),
        lambda msgs: call("update_task", task=last_tool(msgs)["tasks"][0]["ref"], priority="urgent", duration_min=45),
        lambda msgs: Reply(content="Done, it's urgent now." if last_tool(msgs)["saved"] else "Failed"),
    ]
    out = say(authed, conv(authed), "make the venue call urgent and 45 minutes")
    final = out["messages"][-1]
    assert final["content"] == "Done, it's urgent now."
    assert [c["task_id"] for c in final["changes"]] == [t["id"]]
    assert final["changes"][0]["start"]  # read back from the new plan
    saved = db_task(t["id"])
    assert saved.priority == "urgent" and saved.duration_min == 45
    assert not [a for a in out["actions"] if a["status"] == "pending"]


def test_claimed_change_without_a_saved_one_is_flagged(authed, model):
    task(authed, title="Anything")
    model.steps = [Reply(content="I've moved it to Friday.")]
    out = say(authed, conv(authed), "move it to friday")
    assert "no changes were saved" in out["messages"][-1]["content"]


def test_delete_and_bulk_wait_for_confirmation_and_yes_confirms(authed, model):
    setup(authed, ALL_DAY)
    ids = [task(authed, title=f"Admin {i}", duration_min=15)["id"] for i in range(7)]
    model.steps = [
        call("find_tasks", query="admin"),
        lambda msgs: call("bulk_update", tasks=[x["ref"] for x in last_tool(msgs)["tasks"]], priority="low"),
        lambda msgs: Reply(content="Please confirm." if last_tool(msgs).get("needs_confirmation") else "?"),
    ]
    cid = conv(authed)
    out = say(authed, cid, "make all admin tasks low priority")
    assert out["messages"][-1]["content"] == "Please confirm."
    assert all(db_task(i).priority == "medium" for i in ids)
    # A typed "yes" confirms without another model call.
    out = say(authed, cid, "yes")
    assert out["messages"][-1]["role"] == "assistant" and out["messages"][-1]["content"].startswith("Done.")
    assert all(db_task(i).priority == "low" for i in ids)

    model.steps = [
        lambda msgs: call("delete_task", task="T1"),
        lambda msgs: Reply(content="Confirm to delete." if last_tool(msgs).get("needs_confirmation") else "?"),
    ]
    out = say(authed, cid, "delete the first one")
    assert db_task(ids[0]).deleted_at is None
    out = say(authed, cid, "no")
    assert "left everything" in out["messages"][-1]["content"]
    assert db_task(ids[0]).deleted_at is None
    with session_factory()() as db:
        assert {a.status for a in db.scalars(select(AiAction))} == {"confirmed", "cancelled"}


def test_confidential_tasks_are_hidden_from_the_model(authed, model):
    setup(authed, ALL_DAY)
    secret = task(authed, title="Smith v Jones settlement", duration_min=60, confidential=True, notes="client SSN 123")
    allowed = task(authed, title="Doe estate review", duration_min=30, confidential=True, ai_allowed=True)
    plain = task(authed, title="Order printer paper", duration_min=15)
    model.steps = [call("find_tasks"), call("get_agenda", date="today"), call("get_task", task="T1"), Reply(content="ok")]
    say(authed, conv(authed), "what's on my list?")
    sent = json.dumps(model.seen[-1])
    assert "Smith" not in sent and "SSN" not in sent
    assert "Confidential task" in sent
    assert "Doe estate review" in sent and "Order printer paper" in sent
    # Searching by a hidden task's words finds nothing.
    model.steps = [call("find_tasks", query="smith"), Reply(content="none")]
    say(authed, conv(authed), "find smith")
    assert last_tool(model.seen[-1])["count"] == 0

    # A category set to hidden hides even ordinary tasks; set to allowed, confidential ones show.
    cat = authed.post("/categories", json={"name": "Private", "ai_access": "hidden"}).json()
    authed.patch(f"/tasks/{plain['id']}", json={"category_id": cat["id"]})
    model.steps = [call("find_tasks"), Reply(content="ok")]
    say(authed, conv(authed), "list")
    assert "printer" not in json.dumps(model.seen[-1])
    authed.patch(f"/categories/{cat['id']}", json={"ai_access": "allowed"})
    authed.patch(f"/tasks/{secret['id']}", json={"category_id": cat["id"]})
    model.steps = [call("find_tasks"), Reply(content="ok")]
    say(authed, conv(authed), "list")
    assert "Smith v Jones" in json.dumps(model.seen[-1])
    assert allowed


def test_event_titles_stay_private_with_an_external_provider(client, google):
    from .test_google_sync import ev

    connect(client, google)
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    google.put_event("greg.personal@example.com", ev("e1", start, start + timedelta(hours=1), summary="Deposition: Acme"))
    client.post("/calendars/sync")
    for local, visible in ((True, True), (False, False)):
        p = ScriptedProvider([call("get_agenda", date=start.astimezone(NY).date().isoformat()), Reply(content="ok")], local=local)
        set_provider_for_tests(p)
        try:
            say(client, conv(client), "what's on?")
        finally:
            set_provider_for_tests(None)
        assert ("Deposition: Acme" in json.dumps(p.seen[-1])) is visible


def test_provider_failure_is_reported_not_hidden(authed):
    class Down(ScriptedProvider):
        def chat(self, messages, tools):
            raise ProviderError("Couldn't reach Ollama at http://localhost:11434 (ConnectError)")

    set_provider_for_tests(Down())
    try:
        out = say(authed, conv(authed), "hello")
    finally:
        set_provider_for_tests(None)
    assert "isn't available" in out["messages"][-1]["content"]


def test_bad_tool_arguments_come_back_as_errors(authed, model):
    task(authed, title="Thing")
    model.steps = [call("reschedule_task", task="T99", day="2026-13-45"), Reply(content="sorry")]
    say(authed, conv(authed), "move it")
    assert "error" in last_tool(model.seen[-1])


def test_same_day_time_change_applies_without_confirmation(authed, model):
    setup(authed, ALL_DAY)
    t = task(authed, title="Stretch", duration_min=30)
    first = ts(blocks(schedule(authed), t["id"])[0]["start"]).astimezone(NY)
    day = first.date()
    when = (first + timedelta(hours=2)).replace(minute=0)
    if when.date() != day:
        pytest.skip("too close to midnight to test a same-day move")
    model.steps = [
        call("find_tasks", query="stretch"),
        lambda msgs: call("reschedule_task", task=last_tool(msgs)["tasks"][0]["ref"], day=day.isoformat(), time=when.strftime("%H:%M")),
        lambda msgs: Reply(content=json.dumps(last_tool(msgs).get("saved"))),
    ]
    out = say(authed, conv(authed), "move stretch to later today")
    assert out["messages"][-1]["content"] == "true"
    assert ts(blocks(schedule(authed), t["id"])[0]["start"]) == when


def test_what_if_saves_nothing(authed, model):
    setup(authed, ALL_DAY)
    task(authed, title="Existing", duration_min=60)
    model.steps = [call("what_if", add_tasks=[{"title": "Big new job", "duration_min": 600}]), Reply(content="ok")]
    say(authed, conv(authed), "what if I add a big job?")
    res = last_tool(model.seen[-1])
    assert res["saved"] is False and any(c.get("new") for c in res["changes"])
    assert authed.get("/tasks?view=all").json()["total"] == 1


def test_conversations_are_private(client, model):
    from .conftest import login

    login(client, "a@example.com")
    cid = conv(client)
    login(client, "b@example.com")
    assert client.get(f"/assistant/conversations/{cid}").status_code == 404
    assert client.post(f"/assistant/conversations/{cid}/messages", json={"text": "hi"}).status_code == 404
