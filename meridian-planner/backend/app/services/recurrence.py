"""Repeating tasks: turns a task's repeat rule into a series and keeps its occurrences generated.

- Saving a task with a `recurrence` rule creates a RecurringSeries; that task becomes the first occurrence.
- Fixed series generate every occurrence due within `generate_ahead_days`, and always at least one open one.
- Completion-based series generate the next occurrence when the current one is completed.
- Missed occurrences (due time passed, not done) follow the series' policy: keep, skip, or keep only the latest.
- Generation is idempotent: (series_id, occurrence_due) is unique, and dates already generated are never
  generated again, so an occurrence you deleted doesn't come back.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import RecurringSeries, Task, TaskEvent, User
from ..scheduler.recurrence import Rule, RuleError, due_datetime, earliest_start

DONE_STATES = ("done", "cancelled")
TEMPLATE_FIELDS = (
    "title", "description", "priority", "category_id", "project_id", "client", "deadline_type", "duration_min",
    "can_split", "min_session_min", "max_session_min", "auto_schedule", "preferred_time_of_day", "allowed_days",
    "windows", "buffer_before_min", "buffer_after_min", "energy", "calendar_id", "scheduling_profile_id", "location",
    "travel_min", "tags", "links", "confidential", "ai_allowed",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _holidays(user: User) -> tuple[date, ...]:
    raw = ((user.settings or {}).get("schedule") or {}).get("holidays") or []
    return tuple(date.fromisoformat(h) for h in raw)


def rule_of(series: RecurringSeries, user: User) -> Rule:
    return Rule.from_json(series.rule, _holidays(user))


def parse_rule(d: dict, user: User) -> Rule:
    try:
        return Rule.from_json(d, _holidays(user))
    except RuleError as e:
        raise HTTPException(422, str(e)) from None


def _jsonable(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def template_from_task(t: Task) -> dict:
    return {k: _jsonable(getattr(t, k)) for k in TEMPLATE_FIELDS}


def _apply_template(task: Task, tpl: dict) -> None:
    for k, v in tpl.items():
        if k not in TEMPLATE_FIELDS:
            continue
        if k.endswith("_id") and v:
            v = uuid.UUID(str(v))
        setattr(task, k, v)


def _set_dates(task: Task, due: date, rule: Rule, tz: ZoneInfo) -> None:
    task.occurrence_due = due
    task.deadline = due_datetime(due, rule, tz)
    task.earliest_start = earliest_start(due, rule, tz)


def first_due(rule: Rule, anchor: date) -> date:
    if rule.kind == "fixed":
        d = rule.next_fixed(anchor, anchor - timedelta(days=1))
        if d is None:
            raise HTTPException(422, "That repeat rule has no future dates")
        return d
    return rule.adjusted(anchor)


# -- create / update from a task ---------------------------------------------------------------------------------


def attach_series(db: Session, user: User, task: Task) -> RecurringSeries | None:
    """Called after a task is created or its `recurrence` changed. Creates or updates the task's series."""
    rec = task.recurrence
    if not rec or not (rec.get("rrule") or rec.get("kind") == "after_completion"):
        if task.series_id and not rec:
            # Repeat removed: stop the series; this task stays as a one-off.
            s = db.get(RecurringSeries, task.series_id)
            if s:
                s.active = False
        return None
    tz = ZoneInfo(user.timezone)
    if "due_time" not in rec and task.deadline:
        rec = {**rec, "due_time": task.deadline.astimezone(tz).strftime("%H:%M")}
    rule = parse_rule(rec, user)
    if task.series_id:
        series = db.get(RecurringSeries, task.series_id)
        if series:
            update_rule(db, user, series, rec, keep_task=task)
            return series
    if task.deadline:
        anchor = task.deadline.astimezone(tz).date()
        due = first_due(rule, anchor)
    else:
        anchor = datetime.now(tz).date()
        # No date given: a fixed series starts at its next date; a completion-based one is due one interval out.
        due = first_due(rule, anchor) if rule.kind == "fixed" else rule.next_after_completion(anchor)
    series = RecurringSeries(
        user_id=user.id,
        title=task.title,
        template=template_from_task(task),
        rule=_rule_json(rec, rule),
        anchor=anchor,
        missed_policy=rec.get("missed") if rec.get("missed") in ("keep", "skip", "latest_only") else "keep",
        last_generated_due=due,
        generated_count=1,
    )
    db.add(series)
    db.flush()
    task.series_id = series.id
    _set_dates(task, due, rule, tz)
    task.recurrence = series.rule
    db.add(TaskEvent(user_id=user.id, task_id=task.id, actor="system", action="series_created",
                     diff={"series_id": str(series.id), "first_due": due.isoformat()}))
    db.flush()
    generate(db, user, series)
    return series


def _rule_json(rec: dict, rule: Rule) -> dict:
    out = {
        "kind": rule.kind,
        "due_time": rule.due_time,
        "adjust": rule.adjust,
        "text": rec.get("text") or "",
    }
    if rule.kind == "fixed":
        out["rrule"] = rule.rrule
    else:
        out["every"], out["unit"] = rule.every, rule.unit
    if rule.lead_days is not None:
        out["lead_days"] = rule.lead_days
    if rule.until:
        out["until"] = rule.until.isoformat()
    if rule.count:
        out["count"] = rule.count
    if rec.get("missed"):
        out["missed"] = rec["missed"]
    return out


# -- generation --------------------------------------------------------------------------------------------------


def _occurrences(db: Session, series: RecurringSeries) -> list[Task]:
    return list(db.scalars(select(Task).where(Task.series_id == series.id).order_by(Task.occurrence_due)))


def _new_occurrence(db: Session, user: User, series: RecurringSeries, due: date, rule: Rule, tz: ZoneInfo) -> Task | None:
    t = Task(id=uuid.uuid4(), user_id=user.id, source="recurrence", status="todo", checklist=[])
    _apply_template(t, series.template)
    t.title = series.title
    t.duration_min = t.duration_min or 30
    t.remaining_min = t.duration_min
    t.completion_pct = 0
    t.tags = t.tags or []
    t.links = t.links or []
    t.priority = t.priority or "medium"
    t.deadline_type = t.deadline_type or "hard"
    t.recurrence = series.rule
    t.series_id = series.id
    _set_dates(t, due, rule, tz)
    try:
        with db.begin_nested():
            db.add(t)
            db.flush()
    except IntegrityError:
        return None  # already generated (a concurrent run got there first)
    db.add(TaskEvent(user_id=user.id, task_id=t.id, actor="system", action="created",
                     diff={"from_series": str(series.id), "due": due.isoformat()}))
    series.generated_count += 1
    series.last_generated_due = max(series.last_generated_due or due, due)
    return t


def generate(db: Session, user: User, series: RecurringSeries, now: datetime | None = None) -> list[Task]:
    """Create any occurrences that are due to exist, then apply the missed-occurrence policy."""
    if not series.active:
        return []
    now = now or now_utc()
    tz = ZoneInfo(user.timezone)
    today = now.astimezone(tz).date()
    rule = rule_of(series, user)
    occ = _occurrences(db, series)
    have = {t.occurrence_due for t in occ}
    created: list[Task] = []
    last = series.last_generated_due or (series.anchor - timedelta(days=1))

    if rule.kind == "fixed":
        horizon = today + timedelta(days=series.generate_ahead_days)
        for d in rule.fixed_dates(series.anchor, last + timedelta(days=1), horizon):
            if d not in have and d > last:
                t = _new_occurrence(db, user, series, d, rule, tz)
                if t:
                    created.append(t)
        open_future = [t for t in _occurrences(db, series) if t.status not in DONE_STATES and t.deleted_at is None]
        if not open_future:
            # Always keep one open occurrence ahead, even when the next date is beyond the generation window.
            nxt = rule.next_fixed(series.anchor, max(series.last_generated_due or last, today - timedelta(days=1)))
            if nxt and nxt not in have:
                t = _new_occurrence(db, user, series, nxt, rule, tz)
                if t:
                    created.append(t)
            elif nxt is None:
                series.active = False  # the rule has ended (count or until reached)
    _apply_missed_policy(db, user, series, now)
    if created:
        _mark_dirty(user, f"repeating task \"{series.title}\": {len(created)} new")
    return created


def on_completed(db: Session, user: User, task: Task, now: datetime | None = None) -> Task | None:
    """A series occurrence was completed: generate what comes next."""
    if not task.series_id:
        return None
    series = db.get(RecurringSeries, task.series_id)
    if not series or not series.active:
        return None
    now = now or now_utc()
    tz = ZoneInfo(user.timezone)
    rule = rule_of(series, user)
    if rule.kind == "after_completion":
        done_on = (task.completed_at or now).astimezone(tz).date()
        nxt = rule.next_after_completion(done_on)
        if task.occurrence_due and nxt <= task.occurrence_due:
            # Finished early: the next one still comes after this one's own date.
            nxt = rule.adjusted(task.occurrence_due + timedelta(days=1)) if rule.adjust != "previous" else rule.next_after_completion(task.occurrence_due)
        if rule.until and nxt > rule.until:
            series.active = False
            return None
        if rule.count and series.generated_count >= rule.count:
            series.active = False
            return None
        existing = db.scalar(select(Task).where(Task.series_id == series.id, Task.occurrence_due == nxt))
        if existing:
            return existing
        # Never two open occurrences of a completion-based series.
        if any(t.status not in DONE_STATES and t.deleted_at is None and t.id != task.id for t in _occurrences(db, series)):
            return None
        t = _new_occurrence(db, user, series, nxt, rule, tz)
        if t:
            _mark_dirty(user, f"repeating task \"{series.title}\": next one added")
        return t
    created = generate(db, user, series, now)
    return created[0] if created else None


def _apply_missed_policy(db: Session, user: User, series: RecurringSeries, now: datetime) -> None:
    if series.missed_policy == "keep":
        return
    occ = [t for t in _occurrences(db, series) if t.deleted_at is None]
    missed = [t for t in occ if t.status not in DONE_STATES and t.deadline and t.deadline < now]
    if not missed:
        return
    if series.missed_policy == "latest_only":
        # Only the newest occurrence stays open: a missed one closes once a newer one exists.
        newest_due = max(t.occurrence_due for t in occ if t.occurrence_due)
        drop = [t for t in missed if t.occurrence_due and t.occurrence_due < newest_due]
    else:  # skip
        drop = missed
    for t in drop:
        t.status = "cancelled"
        t.completed_at = now
        db.add(TaskEvent(user_id=user.id, task_id=t.id, actor="system", action="missed",
                         diff={"policy": series.missed_policy, "status": {"from": "todo", "to": "cancelled"}}))
    if drop:
        _mark_dirty(user, f"repeating task \"{series.title}\": missed ones closed")


def _mark_dirty(user: User, reason: str) -> None:
    user.schedule_dirty_at = now_utc()
    user.schedule_dirty_reason = reason[:200]


def generate_all(db: Session, now: datetime | None = None) -> int:
    """Worker job: keep every active series topped up."""
    n = 0
    for series in db.scalars(select(RecurringSeries).where(RecurringSeries.active.is_(True))).all():
        user = db.get(User, series.user_id)
        if not user:
            continue
        try:
            n += len(generate(db, user, series, now))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return n


# -- editing -----------------------------------------------------------------------------------------------------


def _unstarted_future(db: Session, series: RecurringSeries, today: date, exclude: Task | None = None) -> list[Task]:
    return [
        t for t in _occurrences(db, series)
        if t.status not in DONE_STATES and t.deleted_at is None and t.completion_pct == 0
        and t.occurrence_due and t.occurrence_due >= today and (exclude is None or t.id != exclude.id)
    ]


def update_rule(db: Session, user: User, series: RecurringSeries, rec: dict, keep_task: Task | None = None) -> None:
    """New repeat rule: unstarted future occurrences are removed and regenerated from the new rule."""
    rule = parse_rule(rec, user)
    tz = ZoneInfo(user.timezone)
    today = now_utc().astimezone(tz).date()
    for t in _unstarted_future(db, series, today, exclude=keep_task):
        db.delete(t)
    db.flush()
    series.rule = _rule_json(rec, rule)
    if rec.get("missed") in ("keep", "skip", "latest_only"):
        series.missed_policy = rec["missed"]
    series.anchor = today
    series.active = True
    remaining = [t for t in _occurrences(db, series) if t.occurrence_due]
    series.last_generated_due = max((t.occurrence_due for t in remaining if t.occurrence_due < today), default=today - timedelta(days=1))
    if keep_task is not None:
        due = first_due(rule, today)
        clash = db.scalar(select(Task).where(Task.series_id == series.id, Task.occurrence_due == due, Task.id != keep_task.id))
        if clash is None:
            _set_dates(keep_task, due, rule, tz)
            series.last_generated_due = max(series.last_generated_due, due)
        keep_task.recurrence = series.rule
    db.flush()
    generate(db, user, series)


def update_series(db: Session, user: User, series: RecurringSeries, patch: dict) -> RecurringSeries:
    tz = ZoneInfo(user.timezone)
    today = now_utc().astimezone(tz).date()
    if "title" in patch and patch["title"]:
        series.title = patch["title"]
    if patch.get("template"):
        tpl = {k: v for k, v in patch["template"].items() if k in TEMPLATE_FIELDS}
        series.template = {**series.template, **_jsonable(tpl)}
    if patch.get("missed_policy"):
        series.missed_policy = patch["missed_policy"]
    if patch.get("generate_ahead_days"):
        series.generate_ahead_days = patch["generate_ahead_days"]
    if "active" in patch and patch["active"] is not None:
        series.active = patch["active"]
    # Push title and template changes to future occurrences that haven't been started.
    if "title" in patch or patch.get("template"):
        for t in _unstarted_future(db, series, today):
            _apply_template(t, series.template)
            t.title = series.title
            t.remaining_min = t.duration_min
            db.add(TaskEvent(user_id=user.id, task_id=t.id, actor="system", action="updated", diff={"series_edit": True}))
    if patch.get("rule"):
        update_rule(db, user, series, patch["rule"])
    else:
        generate(db, user, series)
    _mark_dirty(user, f"repeating task \"{series.title}\" changed")
    db.commit()
    db.refresh(series)
    return series


def delete_series(db: Session, user: User, series: RecurringSeries, delete_open: bool) -> None:
    now = now_utc()
    if delete_open:
        for t in _occurrences(db, series):
            if t.status not in DONE_STATES and t.deleted_at is None:
                t.deleted_at = now
                db.add(TaskEvent(user_id=user.id, task_id=t.id, actor="user", action="deleted", diff={"series_deleted": True}))
    for t in _occurrences(db, series):
        t.recurrence = None
    db.delete(series)
    _mark_dirty(user, "repeating task deleted")
    db.commit()


def upcoming(db: Session, user: User, series: RecurringSeries, n: int = 5) -> list[date]:
    rule = rule_of(series, user)
    tz = ZoneInfo(user.timezone)
    today = now_utc().astimezone(tz).date()
    if rule.kind == "fixed":
        return rule.fixed_dates(series.anchor, today, today + timedelta(days=800))[:n]
    open_ = [t.occurrence_due for t in _occurrences(db, series) if t.status not in DONE_STATES and t.deleted_at is None and t.occurrence_due]
    return sorted(open_)[:n]


def preview(rule: Rule, start: date, n: int) -> list[date]:
    if rule.kind == "fixed":
        return rule.fixed_dates(start, start, start + timedelta(days=3660))[:n]
    out, d = [], start
    for _ in range(n):
        d = rule.next_after_completion(d)
        out.append(d)
    return out
