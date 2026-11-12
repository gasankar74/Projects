"""Scheduling profiles and repeating-task series."""

import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Category, RecurringSeries, SchedulingProfile, Task, User
from ..schemas import ProfileIn, ProfileOut, ProfilePatch, RulePreviewIn, SeriesOut, SeriesPatch
from ..security import current_user
from ..services import recurrence as rec
from ..services.scheduling import mark_dirty
from .schedule import check_work_hours

router = APIRouter(tags=["planning"])


# -- scheduling profiles -----------------------------------------------------------------------------------------


def _own_profile(db: Session, user: User, pid: uuid.UUID) -> SchedulingProfile:
    p = db.get(SchedulingProfile, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Scheduling profile not found")
    return p


def _check_hours(v):
    try:
        check_work_hours(v)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(SchedulingProfile).where(SchedulingProfile.user_id == user.id).order_by(SchedulingProfile.position, SchedulingProfile.name)).all()


@router.post("/profiles", response_model=ProfileOut, status_code=201)
def create_profile(body: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_hours(body.work_hours)
    if db.scalar(select(SchedulingProfile).where(SchedulingProfile.user_id == user.id, SchedulingProfile.name == body.name)):
        raise HTTPException(409, "A profile with that name exists")
    p = SchedulingProfile(user_id=user.id, **body.model_dump())
    db.add(p)
    mark_dirty(user, f"profile \"{body.name}\" added")
    db.commit()
    return p


@router.patch("/profiles/{pid}", response_model=ProfileOut)
def patch_profile(pid: uuid.UUID, body: ProfilePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_profile(db, user, pid)
    fields = body.model_dump(exclude_unset=True)
    if "work_hours" in fields:
        _check_hours(fields["work_hours"])
    for k, v in fields.items():
        if k == "name" and v is None:
            continue
        setattr(p, k, v)
    mark_dirty(user, f"profile \"{p.name}\" changed")
    db.commit()
    return p


@router.delete("/profiles/{pid}", status_code=204)
def delete_profile(pid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_profile(db, user, pid)
    for c in db.scalars(select(Category).where(Category.user_id == user.id, Category.scheduling_profile_id == p.id)):
        c.scheduling_profile_id = None
    db.delete(p)  # tasks fall back to their category's profile or the default hours (ON DELETE SET NULL)
    mark_dirty(user, "profile deleted")
    db.commit()


# -- repeating tasks ---------------------------------------------------------------------------------------------


def _own_series(db: Session, user: User, sid: uuid.UUID) -> RecurringSeries:
    s = db.get(RecurringSeries, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "Repeating task not found")
    return s


def _series_out(db: Session, user: User, s: RecurringSeries) -> SeriesOut:
    o = SeriesOut.model_validate(s)
    r = rec.rule_of(s, user)
    o.description = s.rule.get("text") or r.describe()
    o.upcoming = rec.upcoming(db, user, s) if s.active else []
    o.open_count = len([
        t for t in db.scalars(select(Task).where(Task.series_id == s.id, Task.deleted_at.is_(None)))
        if t.status not in rec.DONE_STATES
    ])
    return o


@router.get("/series", response_model=list[SeriesOut])
def list_series(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(RecurringSeries).where(RecurringSeries.user_id == user.id).order_by(RecurringSeries.title)).all()
    return [_series_out(db, user, s) for s in rows]


@router.get("/series/{sid}", response_model=SeriesOut)
def get_series(sid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _series_out(db, user, _own_series(db, user, sid))


@router.patch("/series/{sid}", response_model=SeriesOut)
def patch_series(sid: uuid.UUID, body: SeriesPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _own_series(db, user, sid)
    s = rec.update_series(db, user, s, body.model_dump(exclude_unset=True))
    return _series_out(db, user, s)


@router.delete("/series/{sid}", status_code=204)
def delete_series(sid: uuid.UUID, delete_open: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rec.delete_series(db, user, _own_series(db, user, sid), delete_open)


@router.post("/series/preview")
def preview(body: RulePreviewIn, user: User = Depends(current_user)):
    rule = rec.parse_rule(body.rule, user)
    start = body.start or datetime.now(timezone.utc).astimezone(ZoneInfo(user.timezone)).date()
    dates: list[date] = rec.preview(rule, start, body.count)
    return {"dates": dates, "description": body.rule.get("text") or rule.describe(), "lead_days": rule.default_lead_days()}
