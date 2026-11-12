"""Time tracking (timer and manual entries) and the daily Top 3."""

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Task, TimeEntry, User
from ..security import current_user
from ..services import tracking

router = APIRouter(tags=["tracking"])


class StartIn(BaseModel):
    task_id: uuid.UUID
    source: str = Field(default="timer", pattern="^(timer|focus)$")


class StopIn(BaseModel):
    reduce_remaining: bool = True
    complete: bool = False


class EntryIn(BaseModel):
    task_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None = None
    minutes: int | None = Field(default=None, ge=1, le=16 * 60)
    note: str | None = Field(default=None, max_length=1000)
    reduce_remaining: bool = False


class EntryPatch(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)


class Top3In(BaseModel):
    day: date
    task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=3)


def _entry_out(db: Session, e: TimeEntry, titles: dict | None = None) -> dict:
    title = (titles or {}).get(e.task_id)
    if title is None:
        t = db.get(Task, e.task_id)
        title = t.title if t else ""
    return {"id": str(e.id), "task_id": str(e.task_id), "title": title, "started_at": e.started_at, "ended_at": e.ended_at,
            "minutes": e.minutes, "source": e.source, "note": e.note, "running": e.ended_at is None}


@router.get("/time/current")
def current(user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = tracking.running(db, user)
    if e is None:
        return None
    out = _entry_out(db, e)
    t = db.get(Task, e.task_id)
    if t:
        out |= {"remaining_min": t.remaining_min, "duration_min": t.duration_min,
                "tracked_min": tracking.tracked_by_task(db, user, [t.id]).get(t.id, 0)}
    return out


@router.post("/time/start")
def start(body: StartIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _entry_out(db, tracking.start(db, user, body.task_id, body.source))


@router.post("/time/stop")
def stop(body: StopIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    body = body or StopIn()
    e = tracking.stop(db, user, body.reduce_remaining, body.complete)
    return _entry_out(db, e) if e else None


@router.get("/time/entries")
def entries(start: datetime | None = None, end: datetime | None = None, task_id: uuid.UUID | None = None,
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(TimeEntry).where(TimeEntry.user_id == user.id)
    if task_id:
        q = q.where(TimeEntry.task_id == task_id)
    if start:
        q = q.where((TimeEntry.ended_at.is_(None)) | (TimeEntry.ended_at > start))
    if end:
        q = q.where(TimeEntry.started_at < end)
    rows = db.scalars(q.order_by(TimeEntry.started_at.desc()).limit(500)).all()
    ids = {e.task_id for e in rows}
    titles = {t.id: t.title for t in db.scalars(select(Task).where(Task.id.in_(ids)))} if ids else {}
    return [_entry_out(db, e, titles) for e in rows]


@router.post("/time/entries", status_code=201)
def add_entry(body: EntryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.started_at.tzinfo is None:
        raise HTTPException(422, "started_at needs a time zone offset")
    end = body.ended_at or (body.started_at + timedelta(minutes=body.minutes) if body.minutes else None)
    if end is None:
        raise HTTPException(422, "Give ended_at or minutes")
    return _entry_out(db, tracking.add_manual(db, user, body.task_id, body.started_at, end, body.note, body.reduce_remaining))


def _own_entry(db: Session, user: User, eid: uuid.UUID) -> TimeEntry:
    e = db.get(TimeEntry, eid)
    if not e or e.user_id != user.id:
        raise HTTPException(404, "Time entry not found")
    return e


@router.patch("/time/entries/{eid}")
def patch_entry(eid: uuid.UUID, body: EntryPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = _own_entry(db, user, eid)
    f = body.model_dump(exclude_unset=True)
    for k, v in f.items():
        setattr(e, k, v)
    if e.ended_at is not None and e.ended_at <= e.started_at:
        raise HTTPException(422, "The end is before the start")
    db.commit()
    return _entry_out(db, e)


@router.delete("/time/entries/{eid}", status_code=204)
def delete_entry(eid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_own_entry(db, user, eid))
    db.commit()


def _today(user: User) -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(user.timezone)).date()


@router.get("/focus/top3")
def get_top3(day: date | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..services import scheduling as sched

    sched.replan_if_dirty(db, user, debounce=False)
    return tracking.top3(db, user, day or _today(user))


@router.put("/focus/top3")
def put_top3(body: Top3In, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return tracking.set_top3(db, user, body.day, body.task_ids)
