"""The planning assistant.

The model only ever sees tool results built here, and it can only change things through the tools below, which call
the same task service and scheduler the UI uses. It never places blocks itself: it changes task facts (deadline,
duration, priority, preferred day, earliest start, a pinned time) and the deterministic engine replans.

Safety rails, all enforced in code rather than in the prompt:
- Large changes need the user's confirmation: deleting, touching more than 5 tasks in one reply, or moving a task to a
  different day. Such a tool call stores a pending AiAction with a preview and writes nothing.
- What the reply says was changed is backed by `changes`, read back from the database after each write. If the model
  claims a change but none was saved, the app says so under the reply.
- Hidden tasks (see privacy.py) are sent as "Confidential task" with timing only. Calendar event titles go to an
  external provider only when the user has opted in.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AiAction, AiConversation, AiMessage, Calendar, CalendarEvent, Category, Project, Task, User
from ..scheduler.engine import TaskInput, plan
from ..schemas import TaskCreate, TaskFields
from ..services import scheduling as sched
from ..services import tasks as svc
from . import privacy
from .providers import Provider, ProviderError, get_provider

MAX_STEPS = 8
HISTORY = 40
BULK_LIMIT = 5
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DATE_FIELDS = {"preferred_date", "earliest_start", "pinned_start"}
AFFIRM = re.compile(r"^\s*(yes|y|yep|yeah|ok|okay|sure|confirm(ed)?|do it|go ahead|please do|sounds good|approved?)\b[\s.!]*(please)?[\s.!]*$", re.I)
DECLINE = re.compile(r"^\s*(no|nope|cancel|don'?t|stop|never ?mind)\b[\s.!]*$", re.I)
CLAIM = re.compile(
    r"\b(I(?:'ve| have)?|I'll|it'?s|it has been|they(?:'ve| have)? been|has been|have been)\s+(?:now\s+|also\s+)?"
    r"(moved|rescheduled|created|added|deleted|removed|updated|changed|marked|completed|scheduled|set|pushed|shifted)\b",
    re.I,
)


class ToolError(Exception):
    pass


# -- tool definitions -------------------------------------------------------------------------------------------

_DAY = {"type": "string", "description": "A date as YYYY-MM-DD (or today, tomorrow, or a weekday name)"}
_WHEN = {"type": "string", "description": "Local date and time as YYYY-MM-DDTHH:MM, or a date (end of working day is used)"}
_REF = {"type": "string", "description": "Task reference such as T3, from an earlier tool result"}
_PRIORITY = {"type": "string", "enum": ["urgent", "high", "medium", "low"]}

TOOLS: list[dict] = [
    {
        "name": "find_tasks",
        "description": "Search the user's tasks. Returns task references (T1, T2, ...) to use with other tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to look for in the title or notes"},
                "status": {"type": "string", "enum": ["open", "done", "all"], "description": "Default open"},
                "due_before": _DAY,
                "category": {"type": "string", "description": "Category name"},
                "project": {"type": "string", "description": "Project name"},
                "limit": {"type": "integer", "description": "Default 20"},
            },
        },
    },
    {
        "name": "get_task",
        "description": "Full details of one task, including when it is planned.",
        "parameters": {"type": "object", "properties": {"task": _REF}, "required": ["task"]},
    },
    {
        "name": "get_agenda",
        "description": "The plan for one day: calendar events and planned work in order, with free time.",
        "parameters": {"type": "object", "properties": {"date": _DAY}},
    },
    {
        "name": "get_risks",
        "description": "Tasks at risk of missing a deadline, tasks that don't fit, and overload.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "create_task",
        "description": "Create a task. The scheduler finds time for it.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "duration_min": {"type": "integer", "description": "Estimated minutes of work, default 30"},
                "deadline": _WHEN,
                "deadline_type": {"type": "string", "enum": ["hard", "soft"]},
                "priority": _PRIORITY,
                "category": {"type": "string"},
                "project": {"type": "string"},
                "preferred_day": _DAY,
                "not_before": _WHEN,
                "notes": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_task",
        "description": "Change facts about a task (estimate, deadline, priority, status, category, project, notes).",
        "parameters": {
            "type": "object",
            "properties": {
                "task": _REF,
                "title": {"type": "string"},
                "duration_min": {"type": "integer"},
                "remaining_min": {"type": "integer", "description": "Minutes of work left"},
                "deadline": _WHEN,
                "deadline_type": {"type": "string", "enum": ["hard", "soft"]},
                "priority": _PRIORITY,
                "status": {"type": "string", "enum": ["todo", "in_progress", "blocked", "done", "cancelled"]},
                "category": {"type": "string"},
                "project": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "reschedule_task",
        "description": (
            "Move a task to another day or time. day only: plan it on that day. day and time: pin it to that start. "
            "not_before: don't start before then. clear: remove earlier day/time choices and let the scheduler decide."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": _REF,
                "day": _DAY,
                "time": {"type": "string", "description": "HH:MM, local"},
                "not_before": _WHEN,
                "clear": {"type": "boolean"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "bulk_update",
        "description": "Apply the same change to several tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {"type": "array", "items": {"type": "string"}},
                "priority": _PRIORITY,
                "deadline": _WHEN,
                "status": {"type": "string", "enum": ["todo", "in_progress", "blocked", "done", "cancelled"]},
                "day": _DAY,
                "not_before": _WHEN,
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task done.",
        "parameters": {"type": "object", "properties": {"task": _REF}, "required": ["task"]},
    },
    {
        "name": "delete_task",
        "description": "Delete a task (always asks the user to confirm).",
        "parameters": {"type": "object", "properties": {"task": _REF}, "required": ["task"]},
    },
    {
        "name": "what_if",
        "description": "Preview the plan with extra tasks, without some tasks, or with extra busy time. Saves nothing.",
        "parameters": {
            "type": "object",
            "properties": {
                "add_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "duration_min": {"type": "integer"}, "deadline": _WHEN},
                        "required": ["title", "duration_min"],
                    },
                },
                "without_tasks": {"type": "array", "items": {"type": "string"}},
                "busy": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"start": _WHEN, "end": _WHEN}, "required": ["start", "end"]},
                },
            },
        },
    },
    {
        "name": "replan",
        "description": "Run the scheduler now and report the result.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def system_prompt(user: User, now: datetime) -> str:
    tz = ZoneInfo(user.timezone)
    local = now.astimezone(tz)
    return f"""You are the planning assistant in Elevate, a task manager that schedules work into the user's calendar.
Now: {local.strftime('%A %Y-%m-%d %H:%M')} ({user.timezone}).

How to work:
- Use tools to look things up and to make changes. Find tasks with find_tasks before changing them; refer to them by their reference (T1, T2, ...).
- The scheduler decides exact times. You change task facts (estimate, deadline, priority, preferred day, earliest start, pinned time) and it replans. Only quote planned times that a tool returned.
- Only say something was changed when a tool result has "saved": true. If a result has "needs_confirmation": true, describe what would change and ask the user to press Confirm (or reply "yes"). Nothing is changed until they do.
- Tasks titled "Confidential task" are private. Don't guess what they are; you may still move or re-estimate them if asked.
- Dates in tool calls: YYYY-MM-DD, times HH:MM, in the user's time zone.
- Keep replies short and plain. Mention deadlines at risk when they matter."""


# -- helpers ----------------------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Assistant:
    def __init__(self, db: Session, user: User, conv: AiConversation, provider: Provider | None = None):
        self.db = db
        self.user = user
        self.conv = conv
        self.provider = provider or get_provider()
        self.tz = ZoneInfo(user.timezone)
        self.turn_touched: set[str] = set()
        self.turn_changes: list[dict] = []
        self.turn_actions: list[AiAction] = []
        self._cats: dict | None = None
        self._projs: dict | None = None

    # -- formatting ---------------------------------------------------------------------------------------------

    def fmt(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.astimezone(self.tz).strftime("%Y-%m-%d %a %H:%M")

    def cats(self) -> dict:
        if self._cats is None:
            self._cats = {c.id: c for c in self.db.scalars(select(Category).where(Category.user_id == self.user.id))}
        return self._cats

    def projs(self) -> dict:
        if self._projs is None:
            self._projs = {p.id: p for p in self.db.scalars(select(Project).where(Project.user_id == self.user.id))}
        return self._projs

    def is_visible(self, t: Task) -> bool:
        return privacy.visible(t, self.user, self.cats().get(t.category_id), self.projs().get(t.project_id))

    def ref(self, task_id) -> str:
        refs = dict(self.conv.refs or {})
        sid = str(task_id)
        for k, v in refs.items():
            if v == sid:
                return k
        k = f"T{len(refs) + 1}"
        refs[k] = sid
        self.conv.refs = refs
        return k

    def resolve(self, r) -> Task:
        key = str(r or "").strip().upper().lstrip("#")
        if key.isdigit():
            key = f"T{key}"
        tid = (self.conv.refs or {}).get(key)
        if not tid:
            raise ToolError(f"Unknown task reference {r!r}. Use find_tasks to get references.")
        t = self.db.get(Task, uuid.UUID(tid))
        if not t or t.user_id != self.user.id or t.deleted_at is not None:
            raise ToolError(f"Task {key} no longer exists.")
        return t

    def label(self, t: Task) -> str:
        return f'"{t.title}"' if self.is_visible(t) else "a confidential task"

    def view(self, t: Task, stats: dict, detail: bool = False) -> dict:
        d: dict = {"ref": self.ref(t.id)}
        vis = self.is_visible(t)
        d["title"] = t.title if vis else "Confidential task"
        if not vis:
            d["hidden"] = True
        d |= {"status": t.status, "priority": t.priority, "duration_min": t.duration_min, "remaining_min": t.remaining_min}
        cat = self.cats().get(t.category_id)
        if cat:
            d["category"] = cat.name
        proj = self.projs().get(t.project_id)
        if proj and vis and privacy.project_visible(proj):
            d["project"] = proj.name
        if t.deadline:
            d["deadline"] = self.fmt(t.deadline)
            d["deadline_type"] = t.deadline_type
        if t.preferred_date:
            d["preferred_day"] = t.preferred_date.isoformat()
        if t.earliest_start:
            d["not_before"] = self.fmt(t.earliest_start)
        if t.pinned_start:
            d["pinned"] = self.fmt(t.pinned_start)
        st = stats.get(str(t.id)) or {}
        if t.status not in svc.DONE_STATES:
            if st.get("start"):
                d["planned"] = f"{self.fmt(_dt(st['start']))} to {self.fmt(_dt(st.get('finish')))}"
            elif t.auto_schedule:
                d["planned"] = "not scheduled"
            if st.get("status") in ("partial", "unscheduled", "late"):
                d["plan_status"] = st["status"]
            if st.get("at_risk"):
                d["at_risk"] = True
        if detail and vis:
            if t.description:
                d["description"] = t.description[:600]
            if t.notes:
                d["notes"] = t.notes[:600]
            if t.checklist:
                d["checklist"] = [{"text": c.text, "done": c.done} for c in t.checklist[:20]]
            if t.tags:
                d["tags"] = t.tags
        return d

    def mask(self, text: str, tasks: list[Task]) -> str:
        """Replace task titles in scheduler messages with references, hiding confidential ones."""
        for t in sorted(tasks, key=lambda x: -len(x.title)):
            if not t.title:
                continue
            rep = f'"{t.title}" ({self.ref(t.id)})' if self.is_visible(t) else f"a confidential task ({self.ref(t.id)})"
            text = text.replace(f'"{t.title}"', rep)
            if not self.is_visible(t):
                text = text.replace(t.title, "a confidential task")
        return text

    # -- parsing ------------------------------------------------------------------------------------------------

    def parse_day(self, s) -> date:
        if s is None or s == "":
            raise ToolError("A date is needed")
        v = str(s).strip().lower()
        today = _now().astimezone(self.tz).date()
        if v == "today":
            return today
        if v == "tomorrow":
            return today + timedelta(days=1)
        if v.startswith("next "):
            v = v[5:]
        for i, name in enumerate(WEEKDAYS):
            if v in (name, name[:3]):
                ahead = (i - today.weekday()) % 7 or 7
                return today + timedelta(days=ahead)
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            raise ToolError(f"Couldn't read the date {s!r}; use YYYY-MM-DD") from None

    def parse_when(self, s, default_time: time = time(17, 0)) -> datetime:
        v = str(s or "").strip()
        if not v:
            raise ToolError("A date or time is needed")
        if len(v) > 10 and ("T" in v or " " in v[10:11]):
            try:
                dt = datetime.fromisoformat(v.replace(" ", "T", 1))
            except ValueError:
                raise ToolError(f"Couldn't read {s!r}; use YYYY-MM-DDTHH:MM") from None
            return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)
        return datetime.combine(self.parse_day(v), default_time, self.tz)

    def parse_clock(self, s) -> time:
        try:
            hh, mm = str(s).strip().split(":")[:2]
            return time(int(hh), int(mm))
        except Exception:
            raise ToolError(f"Couldn't read the time {s!r}; use HH:MM") from None

    def find_named(self, kind: str, name: str):
        items = self.cats() if kind == "category" else {k: p for k, p in self.projs().items() if privacy.project_visible(p) and p.status != "archived"}
        n = name.strip().lower()
        exact = [x for x in items.values() if x.name.lower() == n]
        if exact:
            return exact[0]
        partial = [x for x in items.values() if n in x.name.lower()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(sorted(x.name for x in items.values())) or "none"
        raise ToolError(f"No {kind} called {name!r}. Available: {names}")

    # -- reads --------------------------------------------------------------------------------------------------

    def stats(self) -> dict:
        sched.replan_if_dirty(self.db, self.user, debounce=False)
        return sched.task_stats(self.db, self.user)

    def t_find_tasks(self, a: dict) -> dict:
        status = a.get("status") or "open"
        q = select(Task).where(Task.user_id == self.user.id, Task.deleted_at.is_(None))
        if status == "open":
            q = q.where(Task.status.notin_(svc.DONE_STATES))
        elif status == "done":
            q = q.where(Task.status.in_(svc.DONE_STATES))
        if a.get("due_before"):
            q = q.where(Task.deadline < datetime.combine(self.parse_day(a["due_before"]) + timedelta(days=1), time(0), self.tz))
        if a.get("category"):
            q = q.where(Task.category_id == self.find_named("category", a["category"]).id)
        if a.get("project"):
            q = q.where(Task.project_id == self.find_named("project", a["project"]).id)
        order = Task.completed_at.desc() if status == "done" else Task.deadline.asc().nulls_last()
        rows = self.db.scalars(q.order_by(order, Task.created_at).limit(500)).all()
        words = [w for w in re.split(r"\W+", (a.get("query") or "").lower()) if w]
        if words:
            def hay(t: Task) -> str:
                return " ".join([t.title, t.description or "", t.notes or "", " ".join(t.tags or [])]).lower()

            rows = [t for t in rows if self.is_visible(t) and all(w in hay(t) for w in words)]
        limit = max(1, min(int(a.get("limit") or 20), 50))
        stats = self.stats()
        return {"count": len(rows), "tasks": [self.view(t, stats) for t in rows[:limit]], "more": len(rows) > limit}

    def t_get_task(self, a: dict) -> dict:
        t = self.resolve(a.get("task"))
        stats = self.stats()
        out = self.view(t, stats, detail=True)
        run = sched.current_run(self.db, self.user)
        if run and t.status not in svc.DONE_STATES:
            out["sessions"] = [f"{self.fmt(b.start_at)} to {self.fmt(b.end_at)}" for b in run.blocks if b.task_id == t.id]
            ws = [w for w in run.warnings if w.get("task_id") == str(t.id)]
            if ws:
                out["warnings"] = [self.mask(w["message"], [t]) for w in ws]
        return out

    def t_get_agenda(self, a: dict) -> dict:
        day = self.parse_day(a.get("date") or "today")
        stats = self.stats()
        start = datetime.combine(day, time(0), self.tz).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time(0), self.tz).astimezone(timezone.utc)
        run = sched.current_run(self.db, self.user)
        items: list[tuple[datetime, dict]] = []
        tasks = {}
        for b in (run.blocks if run else []):
            if b.start_at < end and b.end_at > start:
                t = tasks.get(b.task_id) or self.db.get(Task, b.task_id)
                tasks[b.task_id] = t
                if t is None or t.deleted_at is not None:
                    continue
                items.append((b.start_at, {"type": "work", "start": self.fmt(b.start_at), "end": self.fmt(b.end_at), "task": self.ref(t.id),
                                           "title": t.title if self.is_visible(t) else "Confidential task"}))
        show_titles = self.provider.local or self.user.ai_opt_in
        evs = self.db.scalars(
            select(CalendarEvent).join(Calendar, Calendar.id == CalendarEvent.calendar_id).where(
                CalendarEvent.user_id == self.user.id, Calendar.selected.is_(True), CalendarEvent.status != "cancelled",
                CalendarEvent.is_task_block.is_(False), CalendarEvent.start_at < end, CalendarEvent.end_at > start,
            )
        ).all()
        for e in evs:
            entry = {"type": "event", "title": (e.summary or "(no title)") if show_titles else "Busy", "busy": e.is_busy}
            if e.all_day:
                entry["all_day"] = True
            else:
                entry |= {"start": self.fmt(e.start_at), "end": self.fmt(e.end_at)}
            items.append((e.start_at or start, entry))
        items.sort(key=lambda x: x[0])
        summary = next((d for d in (run.days if run else []) if d.get("date") == day.isoformat()), None)
        out = {"date": f"{day.isoformat()} {day.strftime('%A')}", "items": [x[1] for x in items]}
        if summary:
            out |= {"working_min": summary.get("work_min"), "free_min": summary.get("free_min"), "planned_work_min": summary.get("planned_min")}
        if not show_titles and evs:
            out["note"] = "Event titles are hidden from this AI provider."
        return out

    def t_get_risks(self, a: dict) -> dict:
        self.stats()
        run = sched.current_run(self.db, self.user)
        if not run:
            return {"warnings": [], "overload": None}
        ids = {uuid.UUID(w["task_id"]) for w in run.warnings if w.get("task_id")}
        if run.overload:
            ids |= {uuid.UUID(d["task_id"]) for d in run.overload.get("defer", [])}
        tasks = list(self.db.scalars(select(Task).where(Task.id.in_(ids)))) if ids else []
        # Prerequisite titles appear in messages too; mask every task title the user has that's mentioned.
        all_titles = list(self.db.scalars(select(Task).where(Task.user_id == self.user.id, Task.deleted_at.is_(None), Task.status.notin_(svc.DONE_STATES))))
        warnings = [{"kind": w["kind"], "task": self.ref(w["task_id"]) if w.get("task_id") else None, "message": self.mask(w["message"], all_titles)} for w in run.warnings]
        ov = None
        if run.overload:
            by_id = {str(t.id): t for t in tasks}
            ov = {
                "message": self.mask(run.overload.get("message", ""), all_titles),
                "could_defer": [{"task": self.ref(d["task_id"]), "title": by_id[d["task_id"]].title if d["task_id"] in by_id and self.is_visible(by_id[d["task_id"]]) else "Confidential task", "minutes": d.get("minutes")}
                                for d in run.overload.get("defer", [])],
            }
        return {"warnings": warnings, "overload": ov}

    def t_what_if(self, a: dict) -> dict:
        add = []
        for i, x in enumerate(a.get("add_tasks") or []):
            dl = self.parse_when(x["deadline"]) if x.get("deadline") else None
            add.append(TaskInput(id=f"whatif-{i}", title=str(x.get("title") or f"New task {i + 1}"), remaining_min=max(5, int(x.get("duration_min") or 30)), priority="medium", deadline=dl))
        exclude = {str(self.resolve(r).id) for r in a.get("without_tasks") or []}
        busy = [(self.parse_when(b["start"]), self.parse_when(b["end"])) for b in a.get("busy") or []]
        result = sched.dry_run(self.db, self.user, add_tasks=add, exclude=exclude, busy=busy)
        before = _first_starts_run(sched.current_run(self.db, self.user))
        after = _first_starts_plan(result)
        moved = []
        for tid in sorted(set(before) | set(after)):
            if before.get(tid) != after.get(tid):
                if tid.startswith("whatif-"):
                    title = next(t.title for t in add if t.id == tid)
                    moved.append({"task": title, "new": True, "planned": self.fmt(after.get(tid)) or "doesn't fit"})
                else:
                    moved.append({"task": self.ref(tid), "from": self.fmt(before.get(tid)), "to": self.fmt(after.get(tid)) or "doesn't fit"})
        all_titles = list(self.db.scalars(select(Task).where(Task.user_id == self.user.id, Task.deleted_at.is_(None), Task.status.notin_(svc.DONE_STATES))))
        return {
            "saved": False,
            "note": "Preview only. Nothing was changed.",
            "changes": moved[:30],
            "warnings": [self.mask(w.message, all_titles) for w in result.warnings][:20],
        }

    # -- writes -------------------------------------------------------------------------------------------------

    def _patch_from(self, a: dict, t: Task | None, vis: bool) -> dict:
        p: dict = {}
        if a.get("title") is not None:
            if t is not None and not vis:
                raise ToolError("The title of a confidential task can't be changed by the assistant.")
            p["title"] = str(a["title"])[:500]
        for k in ("duration_min", "remaining_min"):
            if a.get(k) is not None:
                p[k] = int(a[k])
        if a.get("deadline"):
            p["deadline"] = self.parse_when(a["deadline"]).isoformat()
        if a.get("deadline_type"):
            p["deadline_type"] = a["deadline_type"]
        if a.get("priority"):
            p["priority"] = a["priority"]
        if a.get("status"):
            p["status"] = a["status"]
        if a.get("category"):
            p["category_id"] = str(self.find_named("category", a["category"]).id)
        if a.get("project"):
            p["project_id"] = str(self.find_named("project", a["project"]).id)
        if a.get("notes") is not None:
            if t is not None and not vis:
                raise ToolError("The notes of a confidential task can't be changed by the assistant.")
            p["notes"] = str(a["notes"])
        if a.get("preferred_day"):
            p["preferred_date"] = self.parse_day(a["preferred_day"]).isoformat()
        if a.get("not_before"):
            p["earliest_start"] = self.parse_when(a["not_before"], time(0)).isoformat()
        return p

    def t_create_task(self, a: dict) -> dict:
        if not (a.get("title") or "").strip():
            raise ToolError("A title is needed")
        p = self._patch_from(a, None, True)
        p.setdefault("duration_min", 30)
        return self.submit([{"op": "create", "patch": p}], f"Create “{p['title']}”")

    def t_update_task(self, a: dict) -> dict:
        t = self.resolve(a.get("task"))
        p = self._patch_from({k: v for k, v in a.items() if k != "task"}, t, self.is_visible(t))
        if not p:
            raise ToolError("Nothing to change")
        confirm = p.get("status") == "cancelled"
        return self.submit([{"op": "update", "task_id": str(t.id), "patch": p}], f"Update {self.label(t)}", force_confirm=confirm)

    def t_reschedule_task(self, a: dict) -> dict:
        t = self.resolve(a.get("task"))
        p: dict = {}
        if a.get("clear"):
            p = {"preferred_date": None, "earliest_start": None, "pinned_start": None}
        if a.get("day"):
            d = self.parse_day(a["day"])
            p["preferred_date"] = d.isoformat()
            if a.get("time"):
                p["pinned_start"] = datetime.combine(d, self.parse_clock(a["time"]), self.tz).isoformat()
                p["earliest_start"] = None
            else:
                p["earliest_start"] = datetime.combine(d, time(0), self.tz).isoformat()
                p["pinned_start"] = None
        elif a.get("time"):
            st = (self.stats().get(str(t.id)) or {}).get("start")
            d = _dt(st).astimezone(self.tz).date() if st else _now().astimezone(self.tz).date()
            p["pinned_start"] = datetime.combine(d, self.parse_clock(a["time"]), self.tz).isoformat()
            p["preferred_date"] = d.isoformat()
        if a.get("not_before"):
            p["earliest_start"] = self.parse_when(a["not_before"], time(0)).isoformat()
        if not p:
            raise ToolError("Give a day, a time, not_before, or clear")
        if p.get("pinned_start") and _dt(p["pinned_start"]) < _now():
            raise ToolError("That time is in the past")
        if t.status in svc.DONE_STATES:
            raise ToolError("That task is already done")
        return self.submit([{"op": "update", "task_id": str(t.id), "patch": p}], f"Reschedule {self.label(t)}")

    def t_bulk_update(self, a: dict) -> dict:
        refs = a.get("tasks") or []
        if not refs:
            raise ToolError("List the tasks to change")
        tasks = [self.resolve(r) for r in refs]
        p: dict = {}
        if a.get("priority"):
            p["priority"] = a["priority"]
        if a.get("deadline"):
            p["deadline"] = self.parse_when(a["deadline"]).isoformat()
        if a.get("status"):
            p["status"] = a["status"]
        if a.get("day"):
            d = self.parse_day(a["day"])
            p |= {"preferred_date": d.isoformat(), "earliest_start": datetime.combine(d, time(0), self.tz).isoformat(), "pinned_start": None}
        if a.get("not_before"):
            p["earliest_start"] = self.parse_when(a["not_before"], time(0)).isoformat()
        if not p:
            raise ToolError("Nothing to change")
        ops = [{"op": "update", "task_id": str(t.id), "patch": dict(p)} for t in {t.id: t for t in tasks}.values()]
        return self.submit(ops, f"Update {len(ops)} tasks", force_confirm=p.get("status") == "cancelled")

    def t_complete_task(self, a: dict) -> dict:
        t = self.resolve(a.get("task"))
        if t.status == "done":
            return {"saved": False, "note": "Already done"}
        return self.submit([{"op": "complete", "task_id": str(t.id)}], f"Complete {self.label(t)}")

    def t_delete_task(self, a: dict) -> dict:
        t = self.resolve(a.get("task"))
        return self.submit([{"op": "delete", "task_id": str(t.id)}], f"Delete {self.label(t)}", force_confirm=True)

    def t_replan(self, a: dict) -> dict:
        run, _ = sched.replan(self.db, self.user, trigger="assistant asked for a replan", force=True)
        stats = run.task_stats or {}
        n_sched = sum(1 for s in stats.values() if s.get("start"))
        risks = self.t_get_risks({})
        return {"saved": True, "note": "Replanned", "tasks_planned": n_sched, "warnings": risks["warnings"][:15], "overload": risks["overload"]}

    # -- submit / confirm ---------------------------------------------------------------------------------------

    def submit(self, ops: list[dict], summary: str, force_confirm: bool = False) -> dict:
        ids = {o["task_id"] for o in ops if o.get("task_id")}
        reason = None
        if force_confirm:
            reason = "deletes or cancels work" if any(o["op"] == "delete" or (o.get("patch") or {}).get("status") == "cancelled" for o in ops) else "large change"
        elif len(self.turn_touched | ids) > BULK_LIMIT:
            reason = f"changes more than {BULK_LIMIT} tasks"
        prev = None
        if reason is None and any(DATE_FIELDS & set((o.get("patch") or {})) for o in ops):
            prev = preview_ops(self.db, self.user, ops)
            if any(m["task_id"] in ids and m["day_changed"] for m in prev["moves"]):
                reason = "moves work to another day"
        if reason:
            prev = prev or preview_ops(self.db, self.user, ops)
            action = AiAction(user_id=self.user.id, conversation_id=self.conv.id, tool=ops[0]["op"], args={"ops": ops},
                              summary=summary, preview=self._preview_for_ui(prev, ops))
            self.db.add(action)
            self.db.flush()
            self.turn_actions.append(action)
            return {
                "saved": False,
                "needs_confirmation": True,
                "why": reason,
                "change": summary.replace("“", '"').replace("”", '"'),
                "preview": self._preview_for_model(prev),
            }
        changes = apply_ops(self.db, self.user, ops)
        self.turn_touched |= ids | {c["task_id"] for c in changes if c.get("task_id")}
        self.turn_changes += changes
        return {"saved": True, "changes": [self.change_for_model(c) for c in changes]}

    def change_for_model(self, c: dict) -> dict:
        t = self.db.get(Task, uuid.UUID(c["task_id"])) if c.get("task_id") else None
        out = {"op": c["op"]}
        if t is not None:
            out["task"] = self.ref(t.id)
            out["title"] = t.title if self.is_visible(t) else "Confidential task"
            out["status"] = t.status
            if c.get("start"):
                out["now_planned"] = f"{self.fmt(_dt(c['start']))} to {self.fmt(_dt(c['end']))}"
            elif c["op"] not in ("delete", "complete") and t.status not in svc.DONE_STATES:
                out["now_planned"] = "not scheduled"
            if c.get("fields"):
                out["fields"] = {k: v for k, v in c["fields"].items() if k not in ("title", "notes") or self.is_visible(t)}
        return out

    def _preview_for_model(self, prev: dict) -> dict:
        moves = []
        for m in prev["moves"][:15]:
            moves.append({"task": self.ref(m["task_id"]), "from": self.fmt(_dt(m["from"])) or "not scheduled", "to": self.fmt(_dt(m["to"])) or "doesn't fit"})
        all_titles = list(self.db.scalars(select(Task).where(Task.user_id == self.user.id, Task.deleted_at.is_(None))))
        return {"moves": moves, "new_warnings": [self.mask(w, all_titles) for w in prev["new_warnings"]][:10]}

    def _preview_for_ui(self, prev: dict, ops: list[dict]) -> dict:
        ids = {uuid.UUID(m["task_id"]) for m in prev["moves"]} | {uuid.UUID(o["task_id"]) for o in ops if o.get("task_id")}
        titles = {str(t.id): t.title for t in self.db.scalars(select(Task).where(Task.id.in_(ids)))} if ids else {}
        items = []
        for o in ops:
            items.append({"op": o["op"], "task_id": o.get("task_id"), "title": titles.get(o.get("task_id") or "", (o.get("patch") or {}).get("title", "")),
                          "fields": o.get("patch") or {}})
        return {
            "items": items,
            "moves": [m | {"title": titles.get(m["task_id"], "")} for m in prev["moves"]],
            "new_warnings": prev["new_warnings"],
        }

    # -- the turn -----------------------------------------------------------------------------------------------

    def call_tool(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"t_{name}", None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            return {"error": f"Unknown tool {name}"}
        if not isinstance(args, dict):
            return {"error": "Arguments must be an object"}
        # Everything before this call is committed, so a failed tool only rolls back its own partial work.
        self.db.commit()
        try:
            return fn(args)
        except ToolError as e:
            self.db.rollback()
            return {"error": str(e)}
        except HTTPException as e:
            self.db.rollback()
            return {"error": str(e.detail)}
        except (ValueError, KeyError, TypeError) as e:
            self.db.rollback()
            return {"error": f"Bad arguments: {e}"}


def _dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def _first_starts_run(run) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for b in (run.blocks if run else []):
        k = str(b.task_id)
        out[k] = min(out.get(k, b.start_at), b.start_at)
    return out


def _first_starts_plan(p) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for b in p.blocks:
        out[b.task_id] = min(out.get(b.task_id, b.start), b.start)
    return out


_TI_FIELDS = {"preferred_date", "earliest_start", "pinned_start", "deadline", "deadline_type", "priority", "remaining_min"}


def preview_ops(db: Session, user: User, ops: list[dict]) -> dict:
    """Plan with the ops applied, without saving: which tasks move, and which warnings are new."""
    snap = sched.build_snapshot(db, user)
    exclude = {o["task_id"] for o in ops if o["op"] in ("delete", "complete")}
    exclude |= {o["task_id"] for o in ops if o["op"] == "update" and (o["patch"].get("status") in ("done", "cancelled", "blocked"))}
    over: dict[str, dict] = {}
    for o in ops:
        if o["op"] != "update":
            continue
        p = o["patch"]
        f: dict = {}
        for k in _TI_FIELDS & set(p):
            v = p[k]
            if k in ("earliest_start", "pinned_start", "deadline"):
                v = _dt(v)
            elif k == "preferred_date":
                v = date.fromisoformat(v) if v else None
            f[k] = v
        if "duration_min" in p and "remaining_min" not in p:
            t = db.get(Task, uuid.UUID(o["task_id"]))
            if t and t.duration_min:
                f["remaining_min"] = max(0, round(p["duration_min"] * t.remaining_min / t.duration_min))
        over[o["task_id"]] = f
    add = []
    for i, o in enumerate(ops):
        if o["op"] == "create":
            p = o["patch"]
            add.append(TaskInput(id=f"new-{i}", title=p.get("title", ""), remaining_min=int(p.get("duration_min") or 30),
                                 priority=p.get("priority") or "medium", deadline=_dt(p.get("deadline")),
                                 preferred_date=date.fromisoformat(p["preferred_date"]) if p.get("preferred_date") else None,
                                 earliest_start=_dt(p.get("earliest_start"))))
    tasks = []
    for t in snap.tasks:
        if t.id in exclude:
            continue
        if t.id in over:
            t = replace(t, **over[t.id])
        tasks.append(t)
    # A task being moved to another day isn't held where it was (apply_ops unlocks its blocks too).
    moved_ids = {o["task_id"] for o in ops if o["op"] == "update" and DATE_FIELDS & set(o["patch"])} | exclude
    prev_blocks = tuple(b for b in snap.previous if b.task_id not in moved_ids)
    result = plan(replace(snap, tasks=tuple(tasks) + tuple(add), previous=prev_blocks))
    cur = sched.current_run(db, user)
    before = _first_starts_run(cur)
    after = _first_starts_plan(result)
    tz = ZoneInfo(user.timezone)
    moves = []
    for tid in sorted((set(before) | set(after)) - {a.id for a in add}):
        b, a_ = before.get(tid), after.get(tid)
        if b != a_ and tid not in exclude:
            bd = b.astimezone(tz).date() if b else None
            ad = a_.astimezone(tz).date() if a_ else None
            moves.append({"task_id": tid, "from": b.isoformat() if b else None, "to": a_.isoformat() if a_ else None, "day_changed": bd != ad})
    old_msgs = {w.get("message") for w in (cur.warnings if cur else [])}
    new_warnings = [w.message for w in result.warnings if w.message not in old_msgs]
    return {"moves": moves, "new_warnings": new_warnings}


def _release_blocks(db: Session, user: User, task_id: uuid.UUID) -> None:
    """Moving a task to another day replaces any lock that held it in place."""
    run = sched.current_run(db, user)
    for b in (run.blocks if run else []):
        if b.task_id == task_id and b.locked:
            b.locked = False
            b.reason = {k: v for k, v in (b.reason or {}).items() if k != "lock_note"}


def apply_ops(db: Session, user: User, ops: list[dict]) -> list[dict]:
    """Write the ops through the task service (actor "ai"), replan, and read back what was saved."""
    changes: list[dict] = []
    touched: list[tuple[str, uuid.UUID, dict]] = []
    for o in ops:
        if o["op"] == "create":
            t = svc.create_task(db, user, TaskCreate.model_validate(o["patch"]), actor="ai", source="ai")
            touched.append(("create", t.id, o["patch"]))
        else:
            t = svc.get_task(db, user, uuid.UUID(o["task_id"]))
            if o["op"] == "update":
                if DATE_FIELDS & set(o["patch"]):
                    _release_blocks(db, user, t.id)
                svc.update_task(db, user, t, TaskFields.model_validate(o["patch"]), actor="ai")
            elif o["op"] == "complete":
                svc.complete_task(db, user, t, actor="ai")
            elif o["op"] == "delete":
                svc.delete_task(db, user, t, actor="ai")
            touched.append((o["op"], t.id, o.get("patch") or {}))
    sched.replan(db, user, trigger="assistant change", force=True)
    run = sched.current_run(db, user)
    blocks: dict[uuid.UUID, list] = {}
    for b in (run.blocks if run else []):
        blocks.setdefault(b.task_id, []).append(b)
    for op, tid, fields in touched:
        t = db.get(Task, tid)
        bl = sorted(blocks.get(tid, []), key=lambda b: b.start_at)
        changes.append({
            "op": op,
            "task_id": str(tid),
            "title": t.title if t else "",
            "status": t.status if t else None,
            "fields": fields,
            "start": bl[0].start_at.isoformat() if bl else None,
            "end": bl[-1].end_at.isoformat() if bl else None,
            "deleted": bool(t and t.deleted_at),
        })
    return changes


# -- conversation plumbing ----------------------------------------------------------------------------------------


def _next_seq(db: Session, conv: AiConversation) -> int:
    return (db.scalar(select(func.max(AiMessage.seq)).where(AiMessage.conversation_id == conv.id)) or 0) + 1


def _add(db: Session, conv: AiConversation, **kw) -> AiMessage:
    m = AiMessage(conversation_id=conv.id, seq=_next_seq(db, conv), **kw)
    db.add(m)
    db.flush()
    return m


def _history(db: Session, conv: AiConversation) -> list[dict]:
    rows = db.scalars(select(AiMessage).where(AiMessage.conversation_id == conv.id).order_by(AiMessage.seq.desc()).limit(HISTORY)).all()[::-1]
    while rows and rows[0].role != "user":
        rows = rows[1:]
    out: list[dict] = []
    for m in rows:
        if m.role == "assistant":
            out.append({"role": "assistant", "content": m.content, "tool_calls": m.tool_calls or []})
        elif m.role == "tool":
            out.append({"role": "tool", "content": m.content, "tool_call_id": m.tool_call_id, "name": m.tool_name})
        elif m.role == "note":
            out.append({"role": "user", "content": f"(App note, not typed by the user) {m.content}"})
        else:
            out.append({"role": "user", "content": m.content})
    return out


def pending_actions(db: Session, conv: AiConversation) -> list[AiAction]:
    return list(db.scalars(select(AiAction).where(AiAction.conversation_id == conv.id, AiAction.status == "pending").order_by(AiAction.created_at)))


def decide(db: Session, user: User, conv: AiConversation, action: AiAction, confirm: bool) -> AiMessage:
    """The user pressed Confirm or Cancel (or replied yes/no). Applies the stored ops on confirm."""
    if action.status != "pending":
        raise HTTPException(409, f"This change was already {action.status}")
    action.decided_at = _now()
    if not confirm:
        action.status = "cancelled"
        msg = _add(db, conv, role="note", content=f"The user cancelled: {action.summary}. Nothing was changed.", action_id=action.id)
        db.commit()
        return msg
    if _now() - action.created_at > timedelta(hours=24):
        action.status = "failed"
        action.result = {"error": "expired"}
        msg = _add(db, conv, role="note", content=f"Not applied (the proposal is more than a day old, ask again): {action.summary}", action_id=action.id)
        db.commit()
        return msg
    try:
        changes = apply_ops(db, user, action.args.get("ops", []))
    except HTTPException as e:
        db.rollback()
        action = db.get(AiAction, action.id)
        action.status = "failed"
        action.decided_at = _now()
        action.result = {"error": str(e.detail)}
        msg = _add(db, conv, role="note", content=f"Couldn't apply “{action.summary}”: {e.detail}", action_id=action.id)
        db.commit()
        return msg
    action.status = "confirmed"
    action.result = {"changes": changes}
    a = Assistant(db, user, conv, provider=_NullProvider())
    model_view = json.dumps([a.change_for_model(c) for c in changes], default=str)
    msg = _add(db, conv, role="note", content=f"The user confirmed “{action.summary}”. Saved: {model_view}", changes=changes, action_id=action.id)
    db.commit()
    return msg


class _NullProvider:
    name = "none"
    model = ""
    local = True


def run_turn(db: Session, user: User, conv: AiConversation, text: str, provider: Provider | None = None) -> list[AiMessage]:
    """Handle one user message. Returns the messages added (user, assistant, tool, notes)."""
    first_seq = _next_seq(db, conv)
    _add(db, conv, role="user", content=text)
    if conv.title in ("", "New conversation"):
        conv.title = text.strip().splitlines()[0][:80] or "New conversation"
    pend = pending_actions(db, conv)
    # A plain "yes"/"no" answers pending proposals directly, without asking the model.
    if pend and (AFFIRM.match(text) or DECLINE.match(text)):
        ok = bool(AFFIRM.match(text))
        for act in pend:
            decide(db, user, conv, act, ok)
        changes = [c for act in pend for c in ((act.result or {}).get("changes") or [])]
        if ok:
            if changes:
                reply = "Done. " + "; ".join(_describe(c, user) for c in changes)
            else:
                reply = "Nothing could be applied; see the note above."
        else:
            reply = "OK, I left everything as it was."
        _add(db, conv, role="assistant", content=reply, changes=changes or None)
        db.commit()
        return _since(db, conv, first_seq)
    db.commit()

    a = Assistant(db, user, conv, provider)
    conv.provider = a.provider.name
    conv.model = a.provider.model
    messages = [{"role": "system", "content": system_prompt(user, _now())}] + _history(db, conv)
    final = None
    try:
        for _ in range(MAX_STEPS):
            reply = a.provider.chat(messages, TOOLS)
            calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in reply.tool_calls]
            msg = _add(db, conv, role="assistant", content=reply.content, tool_calls=calls or None)
            messages.append({"role": "assistant", "content": reply.content, "tool_calls": calls})
            if not calls:
                final = msg
                break
            for c in reply.tool_calls:
                before = len(a.turn_changes)
                n_actions = len(a.turn_actions)
                result = a.call_tool(c.name, c.arguments)
                content = json.dumps(result, default=str, ensure_ascii=False)
                new_changes = a.turn_changes[before:]
                new_action = a.turn_actions[n_actions] if len(a.turn_actions) > n_actions else None
                _add(db, conv, role="tool", content=content, tool_call_id=c.id, tool_name=c.name,
                     changes=new_changes or None, action_id=new_action.id if new_action else None)
                messages.append({"role": "tool", "content": content, "tool_call_id": c.id, "name": c.name})
            db.commit()
        else:
            final = _add(db, conv, role="assistant", content="I stopped after several steps without finishing. Could you say more precisely what you'd like?")
    except ProviderError as e:
        db.rollback()
        final = _add(db, conv, role="assistant", content=f"The assistant isn't available right now: {e}", tool_calls=None)
        final.changes = list(a.turn_changes) or None
    # The app, not the model, states what was saved.
    if final is not None and not final.changes:
        if a.turn_changes:
            final.changes = list(a.turn_changes)
        elif not a.turn_actions and CLAIM.search(final.content or ""):
            final.content = (final.content or "").rstrip() + "\n\n(Note from the app: no changes were saved in this reply.)"
    conv.updated_at = _now()
    db.commit()
    return _since(db, conv, first_seq)


def _since(db: Session, conv: AiConversation, seq: int) -> list[AiMessage]:
    return list(db.scalars(select(AiMessage).where(AiMessage.conversation_id == conv.id, AiMessage.seq >= seq).order_by(AiMessage.seq)))


def _describe(c: dict, user: User) -> str:
    tz = ZoneInfo(user.timezone)
    verb = {"create": "created", "update": "updated", "complete": "completed", "delete": "deleted"}.get(c["op"], c["op"])
    s = f"{verb} “{c.get('title', '')}”"
    if c.get("start") and c["op"] not in ("delete", "complete"):
        s += f", now planned {datetime.fromisoformat(c['start']).astimezone(tz).strftime('%a %d %b %H:%M')}"
    return s
