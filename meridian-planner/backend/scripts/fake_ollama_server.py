"""A tiny rule-based stand-in for Ollama, for demos and browser tests on machines without a model.

It is NOT a language model. It speaks Ollama's /api/chat and /api/tags so the real OllamaProvider code path runs
end to end, and it understands a few fixed phrasings:

  "move <words> to <day>"   find the task, then reschedule it to that day (a weekday name, today, tomorrow or a date)
  "what's on ..."           read today's agenda
  "what's at risk ..."      read the risks
  anything with format=...  (project breakdown) returns a fixed outline

Run:   python -m scripts.fake_ollama_server     (listens on :11435)
Then start the API with MERIDIAN_OLLAMA_URL=http://localhost:11435
"""

import json
import re

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MODEL = "qwen3:8b"
MOVE = re.compile(r"move (?:the |my )?(.+?) to (today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})", re.I)


def _reply(content="", calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in calls]
    return JSONResponse({"model": MODEL, "message": msg, "done": True})


async def tags(request: Request):
    return JSONResponse({"models": [{"name": MODEL}]})


async def chat(request: Request):
    body = await request.json()
    msgs = body.get("messages", [])
    if body.get("format"):
        outline = {"tasks": [
            {"title": "Agree scope and audience", "duration_min": 30},
            {"title": "Draft the outline", "duration_min": 90, "depends_on": [0]},
            {"title": "Write the first draft", "duration_min": 240, "depends_on": [1]},
            {"title": "Review and revise", "duration_min": 120, "depends_on": [2]},
        ]}
        return JSONResponse({"model": MODEL, "message": {"role": "assistant", "content": json.dumps(outline)}, "done": True})
    user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    last = msgs[-1] if msgs else {}
    move = MOVE.search(user)
    if last.get("role") == "user":
        if move:
            return _reply(calls=[("find_tasks", {"query": move.group(1)})])
        if re.search(r"what'?s on|today|agenda", user, re.I):
            return _reply(calls=[("get_agenda", {"date": "today"})])
        if re.search(r"risk|late|deadline", user, re.I):
            return _reply(calls=[("get_risks", {})])
        return _reply("I can move tasks (\"move the board memo to Friday\"), show today's agenda, or check deadline risks.")
    if last.get("role") == "tool":
        data = json.loads(last.get("content") or "{}")
        name = last.get("tool_name")
        if "error" in data:
            return _reply(f"That didn't work: {data['error']}")
        if name == "find_tasks" and move:
            if not data.get("tasks"):
                return _reply(f"I couldn't find a task matching “{move.group(1)}”.")
            return _reply(calls=[("reschedule_task", {"task": data["tasks"][0]["ref"], "day": move.group(2).lower()})])
        if name == "reschedule_task":
            if data.get("needs_confirmation"):
                to = (data.get("preview", {}).get("moves") or [{}])[0].get("to", "the new day")
                return _reply(f"This would move it to {to}. Press Confirm to apply it.")
            if data.get("saved"):
                return _reply("Done. " + "; ".join(f"{c.get('title')} is now planned {c.get('now_planned')}" for c in data.get("changes", [])))
        if name == "get_agenda":
            work = [i for i in data.get("items", []) if i.get("type") == "work"]
            events = [i for i in data.get("items", []) if i.get("type") == "event"]
            lines = [f"- {i.get('start', '')[-5:]} {i['title']}" for i in data.get("items", [])][:10]
            return _reply(f"Today you have {len(events)} events and {len(work)} blocks of planned work:\n" + "\n".join(lines))
        if name == "get_risks":
            ws = data.get("warnings") or []
            return _reply("Nothing is at risk." if not ws else "\n".join(f"- {w['message']}" for w in ws[:5]))
    return _reply("OK.")


app = Starlette(routes=[Route("/api/tags", tags), Route("/api/chat", chat, methods=["POST"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=11435, log_level="warning")
