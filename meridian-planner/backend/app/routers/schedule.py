import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Calendar, CalendarEvent, Category, ScheduledBlock, ScheduleRun, Task, User
from ..scheduler.engine import DAY_NAMES, TaskInput
from ..security import current_user
from ..services import scheduling as sched
from ..services.tasks import DONE_STATES, get_task

router = APIRouter(prefix="/schedule", tags=["schedule"])


# -- shapes -----------------------------------------------------------------------------------------------------


class BlockOut(BaseModel):
    id: uuid.UUID | None = None
    task_id: uuid.UUID
    title: str
    priority: str
    category_id: uuid.UUID | None
    color: str | None
    start: datetime
    end: datetime
    session_index: int
    session_count: int
    locked: bool
    reason: dict


class RunOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    trigger: str
    duration_ms: int
    restored_from_id: uuid.UUID | None
    block_count: int = 0


class ScheduleOut(BaseModel):
    run: RunOut | None
    blocks: list[BlockOut]
    warnings: list[dict]
    overload: dict | None
    days: list[dict]
    dirty: bool
    can_undo: bool = False


class ReplanIn(BaseModel):
    fresh: bool = False


class WhatIfTask(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    duration_min: int = Field(ge=5, le=60 * 24 * 14)
    priority: Literal["urgent", "high", "medium", "low"] = "medium"
    deadline: datetime | None = None
    deadline_type: Literal["hard", "soft"] = "hard"
    can_split: bool = False


class Span(BaseModel):
    start: datetime
    end: datetime


class WhatIfIn(BaseModel):
    add_tasks: list[WhatIfTask] = []
    exclude_task_ids: list[uuid.UUID] = []
    busy: list[Span] = []


class BlockPatch(BaseModel):
    locked: bool | None = None
    start: datetime | None = None


def check_work_hours(v):
    if v is None:
        return v
    for day, spans in v.items():
        if day not in DAY_NAMES:
            raise ValueError(f"Unknown day {day!r}; use {', '.join(DAY_NAMES)}")
        for sp in spans:
            if len(sp) != 2:
                raise ValueError("Each span is [start, end], like [\"09:00\", \"17:00\"]")
            a, b = (time.fromisoformat(x) if x != "24:00" else time(23, 59) for x in sp)
            if a >= b:
                raise ValueError(f"{day}: {sp[0]} must be before {sp[1]}")
    return v


class SettingsPatch(BaseModel):
    work_hours: dict[str, list[list[str]]] | None = None
    horizon_days: int | None = Field(default=None, ge=7, le=180)
    transition_min: int | None = Field(default=None, ge=0, le=120)
    daily_cap_min: int | None = Field(default=None, ge=15, le=24 * 60)
    default_min_session: int | None = Field(default=None, ge=15, le=480)
    freeze_min: int | None = Field(default=None, ge=0, le=240)
    risk_slack_min: int | None = Field(default=None, ge=0, le=60 * 24 * 7)
    weights: dict[str, float] | None = None
    energy_hours: dict[str, list[str]] | None = None
    optimize_hours: int | None = Field(default=None, ge=0, le=168)
    holidays: list[date] | None = None
    write_days: int | None = Field(default=None, ge=1, le=60)
    event_travel_min: int | None = Field(default=None, ge=0, le=240)

    @field_validator("work_hours")
    @classmethod
    def check_hours(cls, v):
        return check_work_hours(v)

    @field_validator("energy_hours")
    @classmethod
    def check_energy(cls, v):
        if v is None:
            return v
        for k, sp in v.items():
            if k not in ("high", "low"):
                raise ValueError("Energy hours are set for high and low")
            check_work_hours({"mon": [sp]})
        return v

    @field_validator("holidays")
    @classmethod
    def iso_holidays(cls, v):
        return None if v is None else sorted({d.isoformat() for d in v})

    @field_validator("weights")
    @classmethod
    def check_weights(cls, v):
        if v is None:
            return v
        bad = set(v) - {"P", "U", "C", "B", "J", "R", "A"}
        if bad:
            raise ValueError(f"Unknown weights: {sorted(bad)}")
        if any(x < 0 for x in v.values()):
            raise ValueError("Weights can't be negative")
        return v


# -- helpers ----------------------------------------------------------------------------------------------------


def _task_meta(db: Session, user: User, ids: set) -> dict:
    if not ids:
        return {}
    cats = {c.id: c.color for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    return {t.id: (t, cats.get(t.category_id)) for t in db.scalars(select(Task).where(Task.id.in_(ids)))}


def _blocks_out(db: Session, user: User, blocks, start: datetime | None = None, end: datetime | None = None) -> list[BlockOut]:
    sel = [b for b in blocks if (start is None or b.end_at > start) and (end is None or b.start_at < end)]
    meta = _task_meta(db, user, {b.task_id for b in sel})
    out = []
    for b in sel:
        t, color = meta.get(b.task_id, (None, None))
        if t is None:
            continue
        out.append(BlockOut(
            id=b.id, task_id=b.task_id, title=t.title, priority=t.priority, category_id=t.category_id, color=color,
            start=b.start_at, end=b.end_at, session_index=b.session_index, session_count=b.session_count, locked=b.locked, reason=b.reason or {},
        ))
    return out


def _run_out(run: ScheduleRun | None) -> RunOut | None:
    if not run:
        return None
    return RunOut(id=run.id, created_at=run.created_at, trigger=run.trigger, duration_ms=run.duration_ms, restored_from_id=run.restored_from_id, block_count=len(run.blocks))


def _schedule_out(db: Session, user: User, run: ScheduleRun | None, start=None, end=None) -> ScheduleOut:
    n_runs = len(db.scalars(select(ScheduleRun.id).where(ScheduleRun.user_id == user.id).limit(2)).all())
    return ScheduleOut(
        run=_run_out(run),
        blocks=_blocks_out(db, user, run.blocks if run else [], start, end),
        warnings=run.warnings if run else [],
        overload=run.overload if run else None,
        days=run.days if run else [],
        dirty=bool(user.schedule_dirty_at),
        can_undo=n_runs > 1,
    )


# -- endpoints --------------------------------------------------------------------------------------------------


@router.get("", response_model=ScheduleOut)
def get_schedule(
    start: datetime | None = None,
    end: datetime | None = None,
    refresh: bool = True,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """The current plan. With refresh (default) a pending change is planned first, so the page is never stale."""
    if refresh:
        sched.replan_if_dirty(db, user, debounce=False)
    run = sched.current_run(db, user)
    if run is None and refresh:
        run, _ = sched.replan(db, user, trigger="first plan")
    return _schedule_out(db, user, run, start, end)


@router.post("/replan", response_model=ScheduleOut)
def replan(body: ReplanIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    body = body or ReplanIn()
    run, _ = sched.replan(db, user, trigger="replan from scratch" if body.fresh else "manual replan", fresh=body.fresh, force=True)
    return _schedule_out(db, user, run)


@router.post("/undo", response_model=ScheduleOut)
def undo(user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = sched.undo(db, user)
    return _schedule_out(db, user, run)


@router.get("/runs", response_model=list[RunOut])
def runs(limit: int = Query(default=20, le=50), user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ScheduleRun).where(ScheduleRun.user_id == user.id).order_by(ScheduleRun.created_at.desc()).limit(limit)).all()
    return [_run_out(r) for r in rows]


@router.post("/what-if")
def what_if(body: WhatIfIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Preview a change (add tasks, drop tasks, add busy time) without saving anything."""
    add = [
        TaskInput(id=f"whatif-{i}", title=t.title, remaining_min=t.duration_min, priority=t.priority, deadline=t.deadline,
                  deadline_type=t.deadline_type, can_split=t.can_split)
        for i, t in enumerate(body.add_tasks)
    ]
    plan = sched.dry_run(db, user, add_tasks=add, exclude={str(x) for x in body.exclude_task_ids}, busy=[(s.start, s.end) for s in body.busy])
    cur = sched.current_run(db, user)
    before: dict[str, datetime] = {}
    for b in (cur.blocks if cur else []):
        k = str(b.task_id)
        before[k] = min(before.get(k, b.start_at), b.start_at)
    after: dict[str, datetime] = {}
    for b in plan.blocks:
        after[b.task_id] = min(after.get(b.task_id, b.start), b.start)
    titles = {t.id: t.title for t in add}
    titles |= {str(t.id): t.title for t in db.scalars(select(Task).where(Task.user_id == user.id, Task.id.in_([uuid.UUID(k) for k in set(before) | set(after) if not k.startswith("whatif")])))}
    changes = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            changes.append({"task_id": k, "title": titles.get(k, ""), "from": before.get(k), "to": after.get(k)})
    return {
        "blocks": plan.to_json()["blocks"],
        "warnings": [w.__dict__ for w in plan.warnings],
        "overload": plan.overload,
        "changes": changes,
    }


@router.patch("/blocks/{block_id}", response_model=ScheduleOut)
def patch_block(block_id: uuid.UUID, body: BlockPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.start is not None:
        if body.start.tzinfo is None:
            raise HTTPException(422, "start needs a time zone offset")
        run = sched.move_block(db, user, block_id, body.start)
        return _schedule_out(db, user, run)
    if body.locked is not None:
        sched.set_block_locked(db, user, block_id, body.locked)
    return _schedule_out(db, user, sched.current_run(db, user))


@router.get("/settings")
def get_settings(user: User = Depends(current_user)):
    return sched.settings_out(user)


@router.patch("/settings")
def patch_settings(body: SettingsPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return sched.update_settings(db, user, body.model_dump(exclude_unset=True))


@router.get("/tasks/{task_id}/why")
def why(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Everything the scheduler knows about one task: score parts, its blocks and the reasons, any warnings."""
    t = get_task(db, user, task_id)
    run = sched.current_run(db, user)
    stats = (run.task_stats if run else {}).get(str(t.id))
    blocks = [b for b in (run.blocks if run else []) if b.task_id == t.id]
    return {
        "task_id": str(t.id),
        "title": t.title,
        "stats": stats,
        "blocks": [b.model_dump() for b in _blocks_out(db, user, blocks)],
        "warnings": [w for w in (run.warnings if run else []) if w.get("task_id") == str(t.id)],
        "auto_schedule": t.auto_schedule,
        "done": t.status in DONE_STATES,
    }


@router.get("/agenda")
def agenda(day: date | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """One day, in order: calendar events and planned work, plus the day's top 3 by score."""
    sched.replan_if_dirty(db, user, debounce=False)
    tz = ZoneInfo(user.timezone)
    day = day or datetime.now(tz).date()
    start = datetime.combine(day, time(0), tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
    run = sched.current_run(db, user)
    blocks = _blocks_out(db, user, run.blocks if run else [], start, end)
    events = db.scalars(
        select(CalendarEvent).join(Calendar, Calendar.id == CalendarEvent.calendar_id).where(
            CalendarEvent.user_id == user.id, Calendar.selected.is_(True), CalendarEvent.status != "cancelled",
            CalendarEvent.is_task_block.is_(False), CalendarEvent.start_at < end, CalendarEvent.end_at > start,
        )
    ).all()
    items = [{"kind": "task", "start": b.start, "end": b.end, "title": b.title, "task_id": str(b.task_id), "block_id": str(b.id), "priority": b.priority, "color": b.color, "locked": b.locked} for b in blocks]
    items += [{"kind": "event", "start": e.start_at, "end": e.end_at, "title": e.summary or "(busy)", "all_day": e.all_day, "busy": e.is_busy, "event_id": str(e.id)} for e in events]
    items.sort(key=lambda x: (x["start"], x["kind"]))
    stats = run.task_stats if run else {}
    seen, top = set(), []
    for b in sorted(blocks, key=lambda b: -(stats.get(str(b.task_id), {}).get("score") or 0)):
        if b.task_id not in seen:
            seen.add(b.task_id)
            top.append({"task_id": str(b.task_id), "title": b.title, "start": b.start, "score": stats.get(str(b.task_id), {}).get("score")})
        if len(top) == 3:
            break
    planned = sum(int((b.end - b.start).total_seconds() // 60) for b in blocks)
    return {"date": day.isoformat(), "items": items, "top3": top, "planned_min": planned}


@router.get("/eisenhower")
def eisenhower(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Open tasks in four boxes. Important = urgent or high priority. Urgent = due within 2 days, at risk, or overdue."""
    sched.replan_if_dirty(db, user, debounce=False)
    stats = sched.task_stats(db, user)
    now = datetime.now(timezone.utc)
    tasks = db.scalars(select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.notin_(DONE_STATES))).all()
    boxes: dict[str, list] = {"do": [], "schedule": [], "delegate": [], "drop": []}
    for t in tasks:
        st = stats.get(str(t.id), {})
        important = t.priority in ("urgent", "high")
        urgent = bool((t.deadline and t.deadline - now < timedelta(days=2)) or st.get("at_risk"))
        key = "do" if important and urgent else "schedule" if important else "delegate" if urgent else "drop"
        boxes[key].append({"id": str(t.id), "title": t.title, "priority": t.priority, "deadline": t.deadline, "score": st.get("score"), "scheduled_start": st.get("start"), "at_risk": bool(st.get("at_risk"))})
    for v in boxes.values():
        v.sort(key=lambda x: -(x["score"] or 0))
    return boxes
