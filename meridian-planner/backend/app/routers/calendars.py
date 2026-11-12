import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Calendar, CalendarEvent, GoogleAccount, User, WatchChannel
from ..schemas import BusyOut, CalendarOut, CalendarPatch, EventOut, GoogleAccountOut
from ..security import current_user
from ..services import calendar_write, push
from ..services import google as g
from ..services.calendar_sync import busy_intervals, sync_account, sync_user
from .auth import google_client

router = APIRouter(tags=["calendars"])


@router.get("/google-accounts", response_model=list[GoogleAccountOut])
def accounts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(GoogleAccount).where(GoogleAccount.user_id == user.id).order_by(GoogleAccount.created_at)).all()


@router.delete("/google-accounts/{aid}", status_code=204)
def disconnect(aid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db), client: g.GoogleClient = Depends(google_client)):
    a = db.get(GoogleAccount, aid)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Account not found")
    push.stop_for_account(db, a, client)
    db.add(AuditLog(user_id=user.id, action="google_disconnected", detail={"email": a.email}))
    db.delete(a)  # cascades to its calendars and stored events; nothing is deleted in Google
    db.commit()


def _cal_out(db: Session, cal: Calendar) -> CalendarOut:
    o = CalendarOut.model_validate(cal)
    o.push_enabled = db.scalar(select(WatchChannel.id).where(WatchChannel.calendar_id == cal.id, WatchChannel.expires_at > datetime.now(timezone.utc)).limit(1)) is not None
    return o


@router.get("/calendars", response_model=list[CalendarOut])
def calendars(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Calendar).where(Calendar.user_id == user.id).order_by(Calendar.primary.desc(), Calendar.summary)
    ).all()
    return [_cal_out(db, c) for c in rows]


@router.patch("/calendars/{cid}", response_model=CalendarOut)
def patch_calendar(cid: uuid.UUID, body: CalendarPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cal = db.get(Calendar, cid)
    if not cal or cal.user_id != user.id:
        raise HTTPException(404, "Calendar not found")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("is_task_target"):
        if not cal.writable:
            raise HTTPException(422, "This calendar is read-only, so task blocks can't be written to it")
        # Exactly one task calendar per user.
        for other in db.scalars(select(Calendar).where(Calendar.user_id == user.id, Calendar.id != cal.id)):
            other.is_task_target = False
    if fields.get("selected") is True and not cal.selected:
        cal.sync_token = None  # newly shown calendar needs a full sync
    for k, v in fields.items():
        setattr(cal, k, v)
    if {"selected", "blocks_time"} & set(fields):
        user.schedule_dirty_at = datetime.now(timezone.utc)
        user.schedule_dirty_reason = f"calendar \"{cal.summary}\" settings changed"
    if "is_task_target" in fields:
        db.flush()
        calendar_write.plan_writes(db, user)  # move blocks to the new calendar (or remove them if turned off)
    db.commit()
    return _cal_out(db, cal)


@router.post("/calendars/sync")
def sync_now(
    if_older_than: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    client: g.GoogleClient = Depends(google_client),
):
    """Sync now. With if_older_than (seconds), only accounts not synced that recently (used when the app regains
    focus, as a fallback for missed push notifications)."""
    if if_older_than is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=if_older_than)
        accts = db.scalars(select(GoogleAccount).where(GoogleAccount.user_id == user.id)).all()
        stale = [a for a in accts if a.status != "needs_reconnect" and (a.last_synced_at is None or a.last_synced_at < cutoff)]
        return {"results": [sync_account(db, a, client) for a in stale], "skipped": len(accts) - len(stale)}
    return {"results": sync_user(db, user, client)}


@router.get("/calendars/write-status")
def write_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Where task blocks are written and how far along the writes are."""
    return calendar_write.write_status(db, user)


@router.post("/calendars/write-now")
def write_now(user: User = Depends(current_user), db: Session = Depends(get_db), client: g.GoogleClient = Depends(google_client)):
    calendar_write.plan_writes(db, user)
    db.commit()
    sent = calendar_write.flush(db, user, client, budget=100)
    return {"sent": sent, **calendar_write.write_status(db, user)}


@router.get("/events", response_model=list[EventOut])
def events(start: datetime, end: datetime, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if (end - start).days > 62:
        raise HTTPException(422, "Range too large (max 62 days)")
    rows = db.execute(
        select(CalendarEvent, Calendar)
        .join(Calendar, Calendar.id == CalendarEvent.calendar_id)
        .where(
            CalendarEvent.user_id == user.id,
            Calendar.selected.is_(True),
            CalendarEvent.status != "cancelled",
            CalendarEvent.is_task_block.is_(False),  # our own task blocks are shown as blocks, not events
            CalendarEvent.start_at < end,
            CalendarEvent.end_at > start,
        )
        .order_by(CalendarEvent.start_at)
    ).all()
    out = []
    for ev, cal in rows:
        o = EventOut.model_validate(ev)
        o.color = cal.color
        o.calendar_name = cal.summary
        o.read_only = not cal.writable
        o.is_busy = ev.is_busy and cal.blocks_time
        if ev.visibility in ("private", "confidential") and not cal.writable:
            o.summary = "Busy"
        out.append(o)
    return out


@router.get("/busy", response_model=list[BusyOut])
def busy(start: datetime, end: datetime, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Merged unavailable time across all selected calendars. The scheduler (Phase 2) builds on this."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "start and end must include a time zone")
    return [BusyOut(start=s, end=e) for s, e in busy_intervals(db, user, start, end)]
