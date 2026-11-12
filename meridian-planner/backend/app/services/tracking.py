"""Time tracking, the daily Top 3, and duration learning."""

from __future__ import annotations

import statistics
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Category, DailyFocus, Task, TimeEntry, User
from ..schemas import TaskFields
from . import scheduling as sched
from . import tasks as svc

MIN_SAMPLES = 5
FACTOR_RANGE = (0.5, 2.0)
LEARN_DAYS = 180


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# -- timer --------------------------------------------------------------------------------------------------------


def running(db: Session, user: User) -> TimeEntry | None:
    return db.scalar(select(TimeEntry).where(TimeEntry.user_id == user.id, TimeEntry.ended_at.is_(None)))


def _log_progress(db: Session, user: User, entry: TimeEntry) -> None:
    """Tracked time counts as work done: it comes off the task's remaining estimate (never below zero)."""
    task = db.get(Task, entry.task_id)
    if task is None or task.deleted_at is not None or task.status in svc.DONE_STATES or entry.minutes <= 0:
        return
    svc.update_task(db, user, task, TaskFields(remaining_min=max(0, task.remaining_min - entry.minutes)))


def stop(db: Session, user: User, reduce_remaining: bool = True, complete: bool = False) -> TimeEntry | None:
    e = running(db, user)
    if e is None:
        return None
    e.ended_at = now_utc()
    db.flush()
    if reduce_remaining:
        _log_progress(db, user, e)
    if complete:
        task = db.get(Task, e.task_id)
        if task and task.deleted_at is None:
            svc.complete_task(db, user, task)
    db.commit()
    return e


def start(db: Session, user: User, task_id: uuid.UUID, source: str = "timer") -> TimeEntry:
    task = svc.get_task(db, user, task_id)
    if task.status in svc.DONE_STATES:
        raise HTTPException(422, "That task is already done")
    cur = running(db, user)
    if cur is not None:
        if cur.task_id == task.id:
            return cur
        stop(db, user)
    e = TimeEntry(user_id=user.id, task_id=task.id, started_at=now_utc(), source=source)
    db.add(e)
    if task.status in ("inbox", "todo"):
        svc.update_task(db, user, task, TaskFields(status="in_progress"))
    db.commit()
    return e


def add_manual(db: Session, user: User, task_id: uuid.UUID, started_at: datetime, ended_at: datetime, note: str | None, reduce_remaining: bool) -> TimeEntry:
    svc.get_task(db, user, task_id)
    if ended_at <= started_at:
        raise HTTPException(422, "The end is before the start")
    if ended_at - started_at > timedelta(hours=16):
        raise HTTPException(422, "One entry can't be longer than 16 hours")
    e = TimeEntry(user_id=user.id, task_id=task_id, started_at=started_at, ended_at=ended_at, source="manual", note=note)
    db.add(e)
    db.flush()
    if reduce_remaining:
        _log_progress(db, user, e)
    db.commit()
    return e


def tracked_by_task(db: Session, user: User, task_ids: list[uuid.UUID] | None = None) -> dict[uuid.UUID, int]:
    """Total tracked minutes per task (finished entries plus a running one so far)."""
    q = select(TimeEntry).where(TimeEntry.user_id == user.id)
    if task_ids is not None:
        if not task_ids:
            return {}
        q = q.where(TimeEntry.task_id.in_(task_ids))
    out: dict[uuid.UUID, int] = {}
    for e in db.scalars(q):
        out[e.task_id] = out.get(e.task_id, 0) + e.minutes
    return out


# -- duration learning --------------------------------------------------------------------------------------------


def estimate_stats(db: Session, user: User, days: int = LEARN_DAYS) -> dict:
    """How long tasks really take compared with their estimate, per category, from finished tasks with tracked time.

    The factor is the median of actual / estimate, used only once a category has MIN_SAMPLES tasks, and kept within
    FACTOR_RANGE so one odd week can't swing the plan."""
    since = now_utc() - timedelta(days=days)
    done = db.scalars(
        select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status == "done", Task.completed_at >= since)
    ).all()
    tracked = tracked_by_task(db, user, [t.id for t in done])
    per_cat: dict[uuid.UUID | None, list[float]] = {}
    samples = []
    for t in done:
        actual = tracked.get(t.id, 0)
        if actual <= 0 or not t.duration_min:
            continue
        r = actual / t.duration_min
        per_cat.setdefault(t.category_id, []).append(r)
        samples.append({"task_id": str(t.id), "title": t.title, "category_id": str(t.category_id) if t.category_id else None,
                        "estimate_min": t.duration_min, "actual_min": actual, "ratio": round(r, 2), "completed_at": t.completed_at})
    cats = {c.id: c for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    rows = []
    for cid, ratios in per_cat.items():
        med = statistics.median(ratios)
        factor = round(min(max(med, FACTOR_RANGE[0]), FACTOR_RANGE[1]), 2)
        c = cats.get(cid)
        rows.append({
            "category_id": str(cid) if cid else None,
            "category": c.name if c else "No category",
            "color": c.color if c else None,
            "samples": len(ratios),
            "median_ratio": round(med, 2),
            "factor": factor if len(ratios) >= MIN_SAMPLES else None,
        })
    rows.sort(key=lambda r: -r["samples"])
    all_ratios = [s["ratio"] for s in samples]
    return {
        "categories": rows,
        "overall_ratio": round(statistics.median(all_ratios), 2) if all_ratios else None,
        "samples": sorted(samples, key=lambda s: s["completed_at"], reverse=True)[:100],
        "min_samples": MIN_SAMPLES,
        "enabled": bool(sched.extra_setting(user, "learn_durations")),
    }


def learned_factors(db: Session, user: User) -> dict[str, float]:
    """Category id (or "none") -> factor, for categories with enough history."""
    out = {}
    for r in estimate_stats(db, user)["categories"]:
        if r["factor"] is not None and r["factor"] != 1.0:
            out[r["category_id"] or "none"] = r["factor"]
    return out


# -- daily Top 3 --------------------------------------------------------------------------------------------------


def _item(t: Task, st: dict, source: str) -> dict:
    return {
        "task_id": str(t.id), "title": t.title, "priority": t.priority, "deadline": t.deadline, "status": t.status,
        "done": t.status in svc.DONE_STATES, "start": st.get("start"), "at_risk": bool(st.get("at_risk")), "score": st.get("score"),
        "why": source,
    }


def suggested_top3(db: Session, user: User, day: date) -> list[dict]:
    """Deterministic: the highest-scoring tasks planned that day; topped up with overdue or at-risk work."""
    tz = ZoneInfo(user.timezone)
    start = datetime.combine(day, time(0), tz)
    end = start + timedelta(days=1)
    run = sched.current_run(db, user)
    stats = run.task_stats if run else {}
    planned = []
    for b in (run.blocks if run else []):
        if start <= b.start_at < end and b.task_id not in planned:
            planned.append(b.task_id)
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(planned)))} if planned else {}
    ranked = sorted((tasks[i] for i in planned if i in tasks and tasks[i].deleted_at is None),
                    key=lambda t: (-(stats.get(str(t.id), {}).get("score") or 0), str(t.id)))
    out = [_item(t, stats.get(str(t.id), {}), "Planned today") for t in ranked[:3]]
    if len(out) < 3 and day == now_utc().astimezone(tz).date():
        seen = {o["task_id"] for o in out}
        extra = db.scalars(select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.notin_(svc.DONE_STATES))).all()
        urgent = [t for t in extra if str(t.id) not in seen and ((t.deadline and t.deadline < end) or stats.get(str(t.id), {}).get("at_risk"))]
        urgent.sort(key=lambda t: (-(stats.get(str(t.id), {}).get("score") or 0), str(t.id)))
        for t in urgent[: 3 - len(out)]:
            out.append(_item(t, stats.get(str(t.id), {}), "Overdue" if t.deadline and t.deadline < now_utc() else "Due soon"))
    return out


def top3(db: Session, user: User, day: date) -> dict:
    pick = db.scalar(select(DailyFocus).where(DailyFocus.user_id == user.id, DailyFocus.day == day))
    if pick and pick.task_ids:
        stats = sched.task_stats(db, user)
        tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(pick.task_ids), Task.deleted_at.is_(None)))}
        items = [_item(tasks[i], stats.get(str(i), {}), "Your pick") for i in pick.task_ids if i in tasks]
        if items:
            return {"day": day.isoformat(), "source": "you", "items": items}
    return {"day": day.isoformat(), "source": "suggested", "items": suggested_top3(db, user, day)}


def set_top3(db: Session, user: User, day: date, task_ids: list[uuid.UUID]) -> dict:
    if len(task_ids) > 3:
        raise HTTPException(422, "Pick up to 3 tasks")
    ids = list(dict.fromkeys(task_ids))
    for i in ids:
        svc.get_task(db, user, i)
    pick = db.scalar(select(DailyFocus).where(DailyFocus.user_id == user.id, DailyFocus.day == day))
    if not ids:
        if pick:
            db.delete(pick)
    elif pick:
        pick.task_ids = ids
    else:
        db.add(DailyFocus(user_id=user.id, day=day, task_ids=ids))
    db.commit()
    return top3(db, user, day)


def tracked_minutes(db: Session, user: User, start: datetime, end: datetime) -> int:
    rows = db.scalars(select(TimeEntry).where(TimeEntry.user_id == user.id, TimeEntry.started_at < end,
                                              func.coalesce(TimeEntry.ended_at, now_utc()) > start)).all()
    total = 0
    for e in rows:
        s = max(e.started_at, start)
        f = min(e.ended_at or now_utc(), end)
        total += max(0, int((f - s).total_seconds() // 60))
    return total
