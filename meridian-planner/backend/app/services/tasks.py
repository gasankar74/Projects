"""Task service: the one place that writes tasks, so the UI, quick add, import and (later) the AI
assistant all go through the same validation and change history."""

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Calendar, Category, ChecklistItem, Project, ScheduleRun, Task, TaskDependency, TaskEvent, User
from ..schemas import TaskCreate, TaskFields, TaskOut

DONE_STATES = ("done", "cancelled")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def record(db: Session, task: Task, action: str, diff: dict | None = None, actor: str = "user") -> None:
    db.add(TaskEvent(user_id=task.user_id, task_id=task.id, actor=actor, action=action, diff=_jsonable(diff or {})))
    # Every task change is a replan trigger (the worker debounces; an unchanged input hash skips the run).
    owner = db.get(User, task.user_id)
    if owner is not None:
        owner.schedule_dirty_at = now_utc()
        owner.schedule_dirty_reason = f"task {action}: {task.title}"[:200]


def get_task(db: Session, user: User, task_id: uuid.UUID) -> Task:
    t = db.get(Task, task_id)
    if not t or t.user_id != user.id or t.deleted_at is not None:
        raise HTTPException(404, "Task not found")
    return t


def _check_owned(db: Session, user: User, model, obj_id: uuid.UUID | None, label: str) -> None:
    if obj_id is None:
        return
    obj = db.get(model, obj_id)
    if not obj or obj.user_id != user.id:
        raise HTTPException(422, f"{label} not found")


def _validate(db: Session, user: User, task: Task) -> None:
    _check_owned(db, user, Category, task.category_id, "Category")
    _check_owned(db, user, Project, task.project_id, "Project")
    if task.calendar_id is not None:
        cal = db.get(Calendar, task.calendar_id)
        if not cal or cal.user_id != user.id:
            raise HTTPException(422, "Calendar not found")
        if not cal.writable:
            raise HTTPException(422, "That calendar is read-only; task blocks can only go to calendars you can edit")
    if task.parent_task_id is not None:
        parent = db.get(Task, task.parent_task_id)
        if not parent or parent.user_id != user.id or parent.id == task.id:
            raise HTTPException(422, "Parent task not found")
    if task.min_session_min and task.max_session_min and task.min_session_min > task.max_session_min:
        raise HTTPException(422, "Minimum session is longer than maximum session")
    if task.min_session_min and task.min_session_min > task.duration_min:
        raise HTTPException(422, "Minimum session is longer than the task")
    if task.earliest_start and task.deadline and task.earliest_start > task.deadline:
        raise HTTPException(422, "Earliest start is after the deadline")


def _apply_progress(task: Task, fields: dict, old_duration: int | None) -> None:
    """Keep duration, remaining time and completion percentage consistent."""
    if "remaining_min" in fields and fields["remaining_min"] is not None:
        task.remaining_min = min(fields["remaining_min"], task.duration_min)
        task.completion_pct = round(100 * (1 - task.remaining_min / task.duration_min)) if task.duration_min else 0
    elif "completion_pct" in fields and fields["completion_pct"] is not None:
        task.remaining_min = round(task.duration_min * (100 - task.completion_pct) / 100)
    elif "duration_min" in fields and old_duration is not None and old_duration != task.duration_min:
        # Duration re-estimated: keep the same share of work done.
        done_share = 1 - (task.remaining_min / old_duration if old_duration else 1)
        task.remaining_min = max(0, round(task.duration_min * (1 - done_share)))
    if task.status not in DONE_STATES and task.completion_pct > 0 and task.status in ("inbox", "todo"):
        task.status = "in_progress"


def create_task(db: Session, user: User, data: TaskCreate, actor: str = "user", source: str = "app") -> Task:
    fields = data.model_dump(exclude_unset=True, exclude={"checklist"})
    if "links" in fields and fields["links"] is not None:
        fields["links"] = [{"title": l.get("title", ""), "url": str(l["url"])} for l in fields["links"]]
    if "windows" in fields and fields["windows"] is not None:
        fields["windows"] = [dict(w) for w in fields["windows"]]
    task = Task(id=uuid.uuid4(), user_id=user.id, source=source)
    for k, v in fields.items():
        if v is not None:
            setattr(task, k, v)
    task.duration_min = task.duration_min or 30
    task.remaining_min = fields.get("remaining_min") if fields.get("remaining_min") is not None else task.duration_min
    task.completion_pct = task.completion_pct or 0
    if "status" not in fields:
        task.status = "todo" if (task.category_id or task.project_id) else "inbox"
    task.tags = task.tags or []
    task.links = task.links or []
    if task.priority is None and task.category_id:
        cat = db.get(Category, task.category_id)
        task.priority = cat.default_priority if cat else "medium"
    task.priority = task.priority or "medium"
    task.deadline_type = task.deadline_type or "hard"
    _apply_progress(task, fields, None)
    _validate(db, user, task)
    db.add(task)
    db.flush()
    for i, text in enumerate(data.checklist):
        db.add(ChecklistItem(task_id=task.id, text=text, position=i))
    record(db, task, "created", {k: v for k, v in fields.items() if v is not None}, actor)
    db.commit()
    db.refresh(task)
    return task


def update_task(db: Session, user: User, task: Task, patch: TaskFields, actor: str = "user") -> Task:
    fields = patch.model_dump(exclude_unset=True)
    if "links" in fields and fields["links"] is not None:
        fields["links"] = [{"title": l.get("title", ""), "url": str(l["url"])} for l in fields["links"]]
    if "windows" in fields and fields["windows"] is not None:
        fields["windows"] = [dict(w) for w in fields["windows"]]
    if "title" in fields and not fields["title"]:
        raise HTTPException(422, "Title is required")
    old_duration = task.duration_min
    diff: dict = {}
    for k, v in fields.items():
        old = getattr(task, k)
        if old != v:
            diff[k] = {"from": old, "to": v}
            setattr(task, k, v)
    if not diff:
        return task
    # Triage: a captured task that gets a home leaves the inbox.
    moved = ("category_id" in diff and task.category_id) or ("project_id" in diff and task.project_id)
    if task.status == "inbox" and "status" not in fields and moved:
        task.status = "todo"
        diff["status"] = {"from": "inbox", "to": "todo"}
    if "status" in fields:
        if task.status == "done" and not task.completed_at:
            task.completed_at = now_utc()
            task.remaining_min = 0
            task.completion_pct = 100
        elif task.status not in DONE_STATES:
            task.completed_at = None
    _apply_progress(task, fields, old_duration)
    _validate(db, user, task)
    record(db, task, "updated", diff, actor)
    db.commit()
    db.refresh(task)
    return task


def complete_task(db: Session, user: User, task: Task, actor: str = "user") -> Task:
    if task.status == "done":
        return task
    prev = task.status
    task.status = "done"
    task.completed_at = now_utc()
    task.remaining_min = 0
    task.completion_pct = 100
    record(db, task, "completed", {"status": {"from": prev, "to": "done"}}, actor)
    for item in task.checklist:
        item.done = True
    db.commit()
    db.refresh(task)
    return task


def reopen_task(db: Session, user: User, task: Task, actor: str = "user") -> Task:
    if task.status not in DONE_STATES:
        return task
    prev = task.status
    task.status = "todo"
    task.completed_at = None
    if task.remaining_min == 0:
        task.remaining_min = task.duration_min
        task.completion_pct = 0
    record(db, task, "reopened", {"status": {"from": prev, "to": "todo"}}, actor)
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, user: User, task: Task, actor: str = "user") -> None:
    task.deleted_at = now_utc()
    record(db, task, "deleted", {}, actor)
    db.commit()


def add_dependency(db: Session, user: User, task: Task, depends_on_id: uuid.UUID) -> None:
    other = get_task(db, user, depends_on_id)
    if other.id == task.id:
        raise HTTPException(422, "A task cannot depend on itself")
    # Reject cycles: walk prerequisites of `other`; if we reach `task`, adding the edge closes a loop.
    stack, seen = [other.id], set()
    while stack:
        cur = stack.pop()
        if cur == task.id:
            raise HTTPException(422, f"That would create a dependency loop with “{other.title}”")
        if cur in seen:
            continue
        seen.add(cur)
        stack += list(db.scalars(select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == cur)))
    exists = db.scalar(
        select(TaskDependency).where(TaskDependency.task_id == task.id, TaskDependency.depends_on_task_id == other.id)
    )
    if not exists:
        db.add(TaskDependency(task_id=task.id, depends_on_task_id=other.id))
        record(db, task, "dependency_added", {"depends_on": str(other.id)})
        db.commit()


def _plan_stats(db: Session, user_id: uuid.UUID) -> dict:
    run = db.scalars(
        select(ScheduleRun).where(ScheduleRun.user_id == user_id, ScheduleRun.applied.is_(True)).order_by(ScheduleRun.created_at.desc()).limit(1)
    ).first()
    return run.task_stats if run else {}


def _apply_stats(out: TaskOut, st: dict | None) -> None:
    if not st or out.status in DONE_STATES:
        return
    out.scheduled_start = st.get("start")
    out.scheduled_end = st.get("finish")
    out.schedule_status = st.get("status")
    out.at_risk = bool(st.get("at_risk"))
    out.score = st.get("score")


def to_out(db: Session, task: Task, deps: dict | None = None, stats: dict | None = None) -> TaskOut:
    if deps is None:
        depends_on = list(db.scalars(select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == task.id)))
        blocking = list(db.scalars(select(TaskDependency.task_id).where(TaskDependency.depends_on_task_id == task.id)))
    else:
        depends_on = deps["on"].get(task.id, [])
        blocking = deps["blocking"].get(task.id, [])
    out = TaskOut.model_validate(task)
    out.depends_on = depends_on
    out.blocking = blocking
    out.overdue = bool(task.deadline and task.status not in DONE_STATES and task.deadline < now_utc())
    if stats is None:
        stats = _plan_stats(db, task.user_id)
    _apply_stats(out, stats.get(str(task.id)))
    return out


def bulk_out(db: Session, tasks: list[Task]) -> list[TaskOut]:
    ids = [t.id for t in tasks]
    on: dict = {}
    blocking: dict = {}
    if ids:
        for d in db.scalars(
            select(TaskDependency).where(
                (TaskDependency.task_id.in_(ids)) | (TaskDependency.depends_on_task_id.in_(ids))
            )
        ):
            on.setdefault(d.task_id, []).append(d.depends_on_task_id)
            blocking.setdefault(d.depends_on_task_id, []).append(d.task_id)
    stats = _plan_stats(db, tasks[0].user_id) if tasks else {}
    return [to_out(db, t, {"on": on, "blocking": blocking}, stats) for t in tasks]
