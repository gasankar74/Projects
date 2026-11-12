"""Connects the pure scheduling engine to the database: builds snapshots, saves runs, undo, locks, triggers."""

from __future__ import annotations

import logging
import time as _time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from ..models import Category, Project, SchedulingProfile, ScheduledBlock, ScheduleRun, Task, TaskDependency, User
from ..scheduler.engine import DEFAULT_WEIGHTS, DEFAULT_WORK_HOURS, BlockInput, Plan, Settings, Snapshot, TaskInput, plan
from .calendar_sync import busy_intervals

log = logging.getLogger(__name__)
DONE_STATES = ("done", "cancelled")
KEEP_RUNS = 50
DEBOUNCE_S = 5

ENGINE_KEYS = {
    "work_hours", "horizon_days", "transition_min", "daily_cap_min", "default_min_session", "freeze_min", "risk_slack_min",
    "weights", "grid_min", "stability_gain_min", "energy_hours", "optimize_hours", "holidays",
}
# Settings used around the engine: how far ahead blocks are written to Google, travel padding around events
# that have a physical location.
EXTRA_DEFAULTS = {"write_days": 14, "event_travel_min": 0}
SETTING_KEYS = ENGINE_KEYS | set(EXTRA_DEFAULTS)
NULLABLE_KEYS = {"daily_cap_min", "energy_hours"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# -- settings ---------------------------------------------------------------------------------------------------


def get_settings(user: User) -> Settings:
    raw = dict((user.settings or {}).get("schedule") or {})
    raw = {k: v for k, v in raw.items() if k in ENGINE_KEYS}
    raw.setdefault("work_hours", DEFAULT_WORK_HOURS)
    raw["weights"] = {**DEFAULT_WEIGHTS, **(raw.get("weights") or {})}
    if "energy_hours" in raw and raw["energy_hours"] is None:
        raw["energy_hours"] = {}
    if "holidays" in raw:
        raw["holidays"] = tuple(sorted(raw["holidays"] or ()))
    return Settings(timezone=user.timezone, **raw)


def extra_setting(user: User, key: str):
    return ((user.settings or {}).get("schedule") or {}).get(key, EXTRA_DEFAULTS[key])


def settings_out(user: User) -> dict:
    s = get_settings(user)
    out = {k: getattr(s, k) for k in sorted(ENGINE_KEYS)} | {"timezone": s.timezone}
    out["holidays"] = list(out["holidays"])
    return out | {k: extra_setting(user, k) for k in EXTRA_DEFAULTS}


def update_settings(db: Session, user: User, patch: dict) -> dict:
    cur = dict((user.settings or {}).get("schedule") or {})
    for k, v in patch.items():
        if k in SETTING_KEYS and v is not None:
            cur[k] = v
        elif k in SETTING_KEYS and v is None and k in NULLABLE_KEYS:
            cur[k] = None
    user.settings = {**(user.settings or {}), "schedule": cur}
    mark_dirty(user, "settings changed")
    db.commit()
    return settings_out(user)


# -- triggers ---------------------------------------------------------------------------------------------------


def mark_dirty(user: User, reason: str) -> None:
    """Something the plan depends on changed. The caller commits; the worker (or the next read) replans."""
    user.schedule_dirty_at = now_utc()
    user.schedule_dirty_reason = reason[:200]


def mark_dirty_id(db: Session, user_id: uuid.UUID, reason: str) -> None:
    u = db.get(User, user_id)
    if u:
        mark_dirty(u, reason)


# -- snapshot ---------------------------------------------------------------------------------------------------


def current_run(db: Session, user: User) -> ScheduleRun | None:
    return db.scalars(
        select(ScheduleRun).where(ScheduleRun.user_id == user.id, ScheduleRun.applied.is_(True)).order_by(ScheduleRun.created_at.desc()).limit(1)
    ).first()


def _task_input(t: Task, deps: list[uuid.UUID], projects: dict, cat_profiles: dict | None = None) -> TaskInput:
    proj = projects.get(t.project_id)
    profile = t.scheduling_profile_id or (cat_profiles or {}).get(t.category_id)
    return TaskInput(
        id=str(t.id),
        title=t.title,
        remaining_min=max(0, t.remaining_min),
        priority=t.priority,
        deadline=t.deadline,
        deadline_type=t.deadline_type,
        earliest_start=t.earliest_start,
        preferred_date=t.preferred_date,
        can_split=t.can_split,
        min_session_min=t.min_session_min,
        max_session_min=t.max_session_min,
        allowed_days=tuple(t.allowed_days) if t.allowed_days is not None else None,
        windows=tuple(t.windows) if t.windows else None,
        preferred_time_of_day=t.preferred_time_of_day,
        pinned_start=t.pinned_start if (t.is_fixed or t.pinned_start) else None,
        depends_on=tuple(str(d) for d in deps),
        category_id=str(t.category_id) if t.category_id else None,
        project_priority=proj.priority if proj else None,
        project_deadline=proj.deadline if proj else None,
        completion_pct=t.completion_pct,
        energy=t.energy,
        buffer_before_min=t.buffer_before_min,
        buffer_after_min=t.buffer_after_min,
        profile_id=str(profile) if profile else None,
        travel_min=t.travel_min or 0,
    )


def build_snapshot(db: Session, user: User, now: datetime | None = None, fresh: bool = False) -> Snapshot:
    now = now or now_utc()
    settings = get_settings(user)
    tasks = db.scalars(
        select(Task).where(
            Task.user_id == user.id,
            Task.deleted_at.is_(None),
            Task.status.notin_(DONE_STATES + ("blocked",)),
            Task.auto_schedule.is_(True),
            Task.remaining_min > 0,
        )
    ).all()
    ids = [t.id for t in tasks]
    deps: dict[uuid.UUID, list[uuid.UUID]] = {}
    prereq_ids: set[uuid.UUID] = set()
    if ids:
        for d in db.scalars(select(TaskDependency).where(TaskDependency.task_id.in_(ids))):
            deps.setdefault(d.task_id, []).append(d.depends_on_task_id)
            prereq_ids.add(d.depends_on_task_id)
    done: set[str] = set()
    if prereq_ids:
        for p in db.scalars(select(Task).where(Task.id.in_(prereq_ids))):
            if p.status in DONE_STATES or p.deleted_at is not None:
                done.add(str(p.id))
    projects = {p.id: p for p in db.scalars(select(Project).where(Project.user_id == user.id))}
    profiles = {
        str(p.id): {"name": p.name, "work_hours": p.work_hours or {}, "daily_cap_min": p.daily_cap_min}
        for p in db.scalars(select(SchedulingProfile).where(SchedulingProfile.user_id == user.id))
    }
    cat_profiles = {
        c.id: c.scheduling_profile_id
        for c in db.scalars(select(Category).where(Category.user_id == user.id, Category.scheduling_profile_id.is_not(None)))
        if str(c.scheduling_profile_id) in profiles
    }
    settings = replace(settings, profiles=profiles)
    busy = busy_intervals(
        db, user, now - timedelta(days=1), now + timedelta(days=settings.horizon_days + 2),
        travel_min=int(extra_setting(user, "event_travel_min") or 0),
    )
    previous: list[BlockInput] = []
    run = current_run(db, user)
    if run:
        open_ids = {str(i) for i in ids}
        for b in run.blocks:
            if b.end_at > now and str(b.task_id) in open_ids:
                note = (b.reason or {}).get("lock_note") if b.locked else None
                previous.append(BlockInput(str(b.task_id), b.start_at, b.end_at, b.locked, note))
    return Snapshot(
        now=now,
        settings=settings,
        tasks=tuple(sorted((_task_input(t, deps.get(t.id, []), projects, cat_profiles) for t in tasks), key=lambda x: x.id)),
        busy=tuple(busy),
        previous=tuple(previous),
        done_task_ids=frozenset(done),
        fresh=fresh,
    )


# -- runs -------------------------------------------------------------------------------------------------------


def replan(
    db: Session,
    user: User,
    trigger: str = "manual",
    fresh: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> tuple[ScheduleRun, Plan | None]:
    """Run the scheduler and save the result as the current plan. Skips the run if nothing changed."""
    t0 = _time.perf_counter()
    # One replan per user at a time (API requests and the worker can race); released at commit.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"replan:{user.id}"})
    db.refresh(user)
    seen_dirty = user.schedule_dirty_at
    snap = build_snapshot(db, user, now, fresh)
    h = snap.input_hash()
    cur = current_run(db, user)
    if cur and cur.input_hash == h and not force and not fresh:
        _clear_dirty(db, user, seen_dirty)
        db.commit()
        return cur, None
    result = plan(snap)
    run = save_plan(db, user, result, trigger, int((_time.perf_counter() - t0) * 1000), seen_dirty=seen_dirty)
    return run, result


_UNSET = object()


def _clear_dirty(db: Session, user: User, seen) -> None:
    """Clear the replan flag only if nobody set it again after this plan's snapshot was taken. A plain assignment
    would overwrite a change committed meanwhile by another request (which this plan didn't see), and that change
    would then never be planned."""
    db.execute(
        update(User)
        .where(User.id == user.id, User.schedule_dirty_at.is_not_distinct_from(seen))
        .values(schedule_dirty_at=None, schedule_dirty_reason=None)
        .execution_options(synchronize_session=False)
    )
    db.expire(user, ["schedule_dirty_at", "schedule_dirty_reason"])


def save_plan(db: Session, user: User, result: Plan, trigger: str, duration_ms: int = 0, restored_from: uuid.UUID | None = None, seen_dirty=_UNSET) -> ScheduleRun:
    run = ScheduleRun(
        user_id=user.id,
        trigger=trigger[:200],
        input_hash=result.input_hash,
        warnings=[{"kind": w.kind, "task_id": w.task_id, "message": w.message, "data": w.data} for w in result.warnings],
        overload=result.overload,
        task_stats=result.to_json()["tasks"],
        days=result.days,
        duration_ms=duration_ms,
        restored_from_id=restored_from,
    )
    db.add(run)
    db.flush()
    for b in result.blocks:
        db.add(ScheduledBlock(
            run_id=run.id, user_id=user.id, task_id=uuid.UUID(b.task_id), start_at=b.start, end_at=b.end,
            session_index=b.session_index, session_count=b.session_count, locked=b.locked, reason=_jsonable(b.reason),
        ))
    db.flush()
    _clear_dirty(db, user, user.schedule_dirty_at if seen_dirty is _UNSET else seen_dirty)
    _prune(db, user)
    _stage_writes(db, user)
    db.commit()
    db.refresh(run)
    return run


def _stage_writes(db: Session, user: User) -> None:
    """Queue the new plan's blocks for writing to Google (the worker sends them)."""
    from . import calendar_write

    calendar_write.plan_writes(db, user)


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _prune(db: Session, user: User) -> None:
    old = db.scalars(
        select(ScheduleRun.id).where(ScheduleRun.user_id == user.id).order_by(ScheduleRun.created_at.desc()).offset(KEEP_RUNS)
    ).all()
    if old:
        db.execute(delete(ScheduleRun).where(ScheduleRun.id.in_(old)))


def dry_run(db: Session, user: User, add_tasks: list[TaskInput] = (), exclude: set[str] = frozenset(), busy: list = (), fresh: bool = False) -> Plan:
    """What-if: plan with changes, without saving anything."""
    snap = build_snapshot(db, user, fresh=fresh)
    snap = replace(
        snap,
        tasks=tuple(t for t in snap.tasks if t.id not in exclude) + tuple(add_tasks),
        busy=tuple(sorted(list(snap.busy) + list(busy))),
    )
    return plan(snap)


def undo(db: Session, user: User) -> ScheduleRun:
    """Restore the plan that came before the current one."""
    cur = current_run(db, user)
    if not cur:
        raise HTTPException(404, "There is no plan to undo yet")
    anchor = db.get(ScheduleRun, cur.restored_from_id) if cur.restored_from_id else cur
    target = db.scalars(
        select(ScheduleRun).where(ScheduleRun.user_id == user.id, ScheduleRun.applied.is_(True), ScheduleRun.created_at < anchor.created_at)
        .order_by(ScheduleRun.created_at.desc()).limit(1)
    ).first() if anchor else None
    if not target:
        raise HTTPException(409, "No earlier plan to go back to")
    run = ScheduleRun(
        user_id=user.id, trigger=f"undo (restored plan from {target.created_at.isoformat(timespec='minutes')})",
        input_hash="", warnings=target.warnings, overload=target.overload, task_stats=target.task_stats, days=target.days,
        restored_from_id=target.id,
    )
    db.add(run)
    db.flush()
    open_ids = {t for t in db.scalars(select(Task.id).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.notin_(DONE_STATES)))}
    for b in target.blocks:
        if b.task_id in open_ids:
            db.add(ScheduledBlock(run_id=run.id, user_id=user.id, task_id=b.task_id, start_at=b.start_at, end_at=b.end_at,
                                  session_index=b.session_index, session_count=b.session_count, locked=b.locked, reason=b.reason))
    db.flush()
    db.expire(run)
    run.input_hash = build_snapshot(db, user).input_hash()
    db.flush()
    _clear_dirty(db, user, user.schedule_dirty_at)
    _prune(db, user)
    db.flush()
    _stage_writes(db, user)
    db.commit()
    db.refresh(run)
    return run


def get_block(db: Session, user: User, block_id: uuid.UUID) -> ScheduledBlock:
    cur = current_run(db, user)
    b = db.get(ScheduledBlock, block_id)
    if not b or b.user_id != user.id or not cur or b.run_id != cur.id:
        raise HTTPException(404, "Block not found in the current plan")
    return b


def set_block_locked(db: Session, user: User, block_id: uuid.UUID, locked: bool) -> ScheduledBlock:
    b = get_block(db, user, block_id)
    b.locked = locked
    why = list((b.reason or {}).get("why", []))
    note = "Locked by you"
    why = [w for w in why if w != note]
    if locked:
        why.insert(0, note)
    b.reason = {**{k: v for k, v in (b.reason or {}).items() if k != "lock_note"}, "why": why}
    mark_dirty(user, "block locked" if locked else "block unlocked")
    db.commit()
    return b


def move_block(db: Session, user: User, block_id: uuid.UUID, start: datetime) -> ScheduleRun:
    """Manual drag: put the block at a new time, lock it there, and replan everything else around it."""
    b = get_block(db, user, block_id)
    dur = b.end_at - b.start_at
    b.start_at = start
    b.end_at = start + dur
    b.locked = True
    b.reason = {**(b.reason or {}), "why": ["Moved and locked by you"], "bound_by": "locked", "lock_note": "Moved and locked by you"}
    db.commit()
    run, _ = replan(db, user, trigger="block moved", force=True)
    return run


def replan_if_dirty(db: Session, user: User, debounce: bool = True) -> ScheduleRun | None:
    if not user.schedule_dirty_at:
        return None
    if debounce and now_utc() - user.schedule_dirty_at < timedelta(seconds=DEBOUNCE_S):
        return None
    run, _ = replan(db, user, trigger=user.schedule_dirty_reason or "change")
    return run


def task_stats(db: Session, user: User) -> dict[str, dict]:
    run = current_run(db, user)
    return run.task_stats if run else {}
