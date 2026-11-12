"""Weekly dashboard, workload, and reports. Read-only views over tasks, the current plan and tracked time."""

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Category, Project, Task, TimeEntry, User
from ..security import current_user
from ..services import scheduling as sched
from ..services import tracking
from ..services.tasks import DONE_STATES

router = APIRouter(tags=["insights"])
NO_KEY = "none"


def _tz(user: User) -> ZoneInfo:
    return ZoneInfo(user.timezone)


def _local_range(user: User, start: date, days: int) -> tuple[datetime, datetime]:
    tz = _tz(user)
    return datetime.combine(start, time(0), tz), datetime.combine(start + timedelta(days=days), time(0), tz)


def _clip_minutes(s: datetime, e: datetime, lo: datetime, hi: datetime) -> int:
    a, b = max(s, lo), min(e, hi)
    return max(0, int((b - a).total_seconds() // 60))


def _groups(db: Session, user: User):
    cats = {c.id: c for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    projs = {p.id: p for p in db.scalars(select(Project).where(Project.user_id == user.id))}
    return cats, projs


def _key_label(t: Task | None, group: str, cats: dict, projs: dict, tz: ZoneInfo, at: datetime | None = None) -> tuple[str, str, str | None]:
    if group == "day":
        d = at.astimezone(tz).date()
        return d.isoformat(), d.strftime("%a %d %b"), None
    if group == "task":
        return (str(t.id), t.title, None) if t else (NO_KEY, "Deleted task", None)
    if group == "project":
        p = projs.get(t.project_id) if t else None
        return (str(p.id), p.name, p.color) if p else (NO_KEY, "No project", None)
    c = cats.get(t.category_id) if t else None
    return (str(c.id), c.name, c.color) if c else (NO_KEY, "No category", None)


def _entries(db: Session, user: User, lo: datetime, hi: datetime) -> list[TimeEntry]:
    now = datetime.now(timezone.utc)
    return list(db.scalars(select(TimeEntry).where(
        TimeEntry.user_id == user.id, TimeEntry.started_at < hi, func.coalesce(TimeEntry.ended_at, now) > lo,
    ).order_by(TimeEntry.started_at)))


def _tracked(db: Session, user: User, lo: datetime, hi: datetime, group: str) -> list[dict]:
    tz = _tz(user)
    cats, projs = _groups(db, user)
    now = datetime.now(timezone.utc)
    entries = _entries(db, user, lo, hi)
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_({e.task_id for e in entries})))} if entries else {}
    rows: dict[str, dict] = {}
    for e in entries:
        t = tasks.get(e.task_id)
        if group == "day":
            # Split an entry across midnight into the days it touches.
            cur = max(e.started_at, lo).astimezone(tz)
            end = min(e.ended_at or now, hi)
            while cur < end:
                nxt = datetime.combine(cur.date() + timedelta(days=1), time(0), tz)
                k, label, color = _key_label(t, "day", cats, projs, tz, cur)
                r = rows.setdefault(k, {"key": k, "label": label, "color": color, "minutes": 0})
                r["minutes"] += _clip_minutes(cur, min(nxt, end), lo, hi)
                cur = nxt
            continue
        k, label, color = _key_label(t, group, cats, projs, tz)
        r = rows.setdefault(k, {"key": k, "label": label, "color": color, "minutes": 0})
        r["minutes"] += _clip_minutes(e.started_at, e.ended_at or now, lo, hi)
    out = [r for r in rows.values() if r["minutes"] > 0]
    out.sort(key=lambda r: r["key"] if group == "day" else -r["minutes"])
    return out


def _monday(user: User) -> date:
    today = datetime.now(timezone.utc).astimezone(_tz(user)).date()
    return today - timedelta(days=today.weekday())


@router.get("/dashboard/week")
def week(start: date | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """One week at a glance: capacity, planned and tracked time, what got done, deadlines and risks."""
    sched.replan_if_dirty(db, user, debounce=False)
    start = start or _monday(user)
    lo, hi = _local_range(user, start, 7)
    tz = _tz(user)
    run = sched.current_run(db, user)
    stats = run.task_stats if run else {}
    plan_days = {d["date"]: d for d in (run.days if run else [])}
    tracked_by_day = {r["key"]: r["minutes"] for r in _tracked(db, user, lo, hi, "day")}
    done = db.scalars(select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status == "done",
                                         Task.completed_at >= lo, Task.completed_at < hi).order_by(Task.completed_at.desc())).all()
    done_by_day: dict[str, int] = {}
    for t in done:
        k = t.completed_at.astimezone(tz).date().isoformat()
        done_by_day[k] = done_by_day.get(k, 0) + 1
    # Planned minutes come from the current plan's blocks (past days in the week show what is still planned).
    planned_by_day: dict[str, int] = {}
    for b in (run.blocks if run else []):
        if b.start_at < hi and b.end_at > lo:
            k = b.start_at.astimezone(tz).date().isoformat()
            planned_by_day[k] = planned_by_day.get(k, 0) + _clip_minutes(b.start_at, b.end_at, lo, hi)
    days = []
    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        pd = plan_days.get(d) or {}
        days.append({"date": d, "work_min": pd.get("work_min"), "free_min": pd.get("free_min"), "planned_min": planned_by_day.get(d, 0),
                     "tracked_min": tracked_by_day.get(d, 0), "done": done_by_day.get(d, 0)})
    open_tasks = db.scalars(select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.notin_(DONE_STATES))).all()
    now = datetime.now(timezone.utc)
    deadlines = sorted(
        ({"task_id": str(t.id), "title": t.title, "deadline": t.deadline, "deadline_type": t.deadline_type, "priority": t.priority,
          "finish": (stats.get(str(t.id)) or {}).get("finish"), "at_risk": bool((stats.get(str(t.id)) or {}).get("at_risk"))}
         for t in open_tasks if t.deadline and lo <= t.deadline < hi),
        key=lambda x: x["deadline"],
    )
    risks = [w for w in (run.warnings if run else []) if w["kind"] in ("cannot_meet_deadline", "at_risk", "will_miss_soft_deadline", "unscheduled")]
    created = db.scalar(select(func.count()).select_from(Task).where(Task.user_id == user.id, Task.created_at >= lo, Task.created_at < hi, Task.deleted_at.is_(None)))
    tracked_ids = {e.task_id for e in _entries(db, user, lo, hi)}
    acc = []
    if done:
        tracked_total = tracking.tracked_by_task(db, user, [t.id for t in done])
        for t in done:
            if tracked_total.get(t.id):
                acc.append({"task_id": str(t.id), "title": t.title, "estimate_min": t.duration_min, "actual_min": tracked_total[t.id]})
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(days=6)).isoformat(),
        "days": days,
        "totals": {
            "work_min": sum(d["work_min"] or 0 for d in days),
            "planned_min": sum(d["planned_min"] for d in days),
            "tracked_min": sum(d["tracked_min"] for d in days),
            "done": len(done),
            "created": created or 0,
            "overdue": sum(1 for t in open_tasks if t.deadline and t.deadline < now),
            "tasks_worked_on": len(tracked_ids),
        },
        "tracked_by_category": _tracked(db, user, lo, hi, "category"),
        "deadlines": deadlines,
        "risks": risks[:20],
        "overload": run.overload if run else None,
        "done_tasks": [{"task_id": str(t.id), "title": t.title, "completed_at": t.completed_at} for t in done[:15]],
        "estimate_vs_actual": acc[:15],
    }


@router.get("/workload")
def workload(start: date | None = None, days: int = Query(default=14, ge=1, le=56), group: Literal["category", "project"] = "category",
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Planned work per day against working time, split by category or project."""
    sched.replan_if_dirty(db, user, debounce=False)
    tz = _tz(user)
    start = start or datetime.now(timezone.utc).astimezone(tz).date()
    lo, hi = _local_range(user, start, days)
    run = sched.current_run(db, user)
    cats, projs = _groups(db, user)
    blocks = [b for b in (run.blocks if run else []) if b.start_at < hi and b.end_at > lo]
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_({b.task_id for b in blocks})))} if blocks else {}
    plan_days = {d["date"]: d for d in (run.days if run else [])}
    groups: dict[str, dict] = {}
    per_day: dict[str, dict[str, int]] = {}
    for b in blocks:
        t = tasks.get(b.task_id)
        k, label, color = _key_label(t, group, cats, projs, tz)
        groups.setdefault(k, {"key": k, "label": label, "color": color, "minutes": 0})
        m = _clip_minutes(b.start_at, b.end_at, lo, hi)
        groups[k]["minutes"] += m
        d = b.start_at.astimezone(tz).date().isoformat()
        per_day.setdefault(d, {})
        per_day[d][k] = per_day[d].get(k, 0) + m
    out_days = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        pd = plan_days.get(d) or {}
        by = per_day.get(d, {})
        out_days.append({"date": d, "work_min": pd.get("work_min") or 0, "free_min": pd.get("free_min"), "planned_min": sum(by.values()), "by_group": by})
    gl = sorted(groups.values(), key=lambda g: -g["minutes"])
    return {"start": start.isoformat(), "days": out_days, "groups": gl, "group": group}


@router.get("/reports/summary")
def summary(start: date, end: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Tracked time by category, project and day; tasks completed; estimate against actual for finished tasks."""
    if end < start:
        raise HTTPException(422, "The end is before the start")
    if (end - start).days > 366:
        raise HTTPException(422, "Pick a range of a year or less")
    lo, hi = _local_range(user, start, (end - start).days + 1)
    tz = _tz(user)
    done = db.scalars(select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status == "done",
                                         Task.completed_at >= lo, Task.completed_at < hi)).all()
    by_day: dict[str, int] = {}
    for t in done:
        k = t.completed_at.astimezone(tz).date().isoformat()
        by_day[k] = by_day.get(k, 0) + 1
    tracked_total = tracking.tracked_by_task(db, user, [t.id for t in done])
    cats, _ = _groups(db, user)
    est = []
    for t in done:
        a = tracked_total.get(t.id, 0)
        if a:
            c = cats.get(t.category_id)
            est.append({"task_id": str(t.id), "title": t.title, "category": c.name if c else None, "estimate_min": t.duration_min,
                        "actual_min": a, "ratio": round(a / t.duration_min, 2) if t.duration_min else None, "completed_at": t.completed_at})
    est.sort(key=lambda r: r["completed_at"], reverse=True)
    by_cat = _tracked(db, user, lo, hi, "category")
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tracked_total_min": sum(r["minutes"] for r in by_cat),
        "tracked_by_category": by_cat,
        "tracked_by_project": _tracked(db, user, lo, hi, "project"),
        "tracked_by_day": _tracked(db, user, lo, hi, "day"),
        "tracked_by_task": _tracked(db, user, lo, hi, "task")[:25],
        "completed": len(done),
        "completed_by_day": [{"date": k, "count": v} for k, v in sorted(by_day.items())],
        "estimates": est[:100],
    }


@router.get("/reports/estimates")
def estimates(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return tracking.estimate_stats(db, user)


@router.get("/reports/time.csv")
def time_csv(start: date, end: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lo, hi = _local_range(user, start, (end - start).days + 1)
    tz = _tz(user)
    entries = _entries(db, user, lo, hi)
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_({e.task_id for e in entries})))} if entries else {}
    cats, projs = _groups(db, user)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "start", "end", "minutes", "task", "category", "project", "source", "note"])
    for e in entries:
        t = tasks.get(e.task_id)
        c = cats.get(t.category_id) if t else None
        p = projs.get(t.project_id) if t else None
        s = e.started_at.astimezone(tz)
        f = e.ended_at.astimezone(tz) if e.ended_at else None
        w.writerow([s.date().isoformat(), s.strftime("%H:%M"), f.strftime("%H:%M") if f else "running", e.minutes,
                    t.title if t else "", c.name if c else "", p.name if p else "", e.source, e.note or ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="meridian-time-{start}-{end}.csv"'})

