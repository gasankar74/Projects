"""Writes planned task blocks to the user's chosen Google calendar, and reads changes back.

Flow
  1. plan_writes(): after every saved plan, compare the plan's blocks (from now to `write_days` ahead) with the
     BlockEventLink rows and mark what must be created, moved or removed. No network.
  2. flush(): the worker sends pending writes to Google, oldest first, inside a per-minute budget, with
     exponential backoff on errors. Each link has a fixed event id, so a retried insert gets 409 (already exists)
     and is turned into an update: never a duplicate event.
  3. on_synced_block(): when calendar sync sees one of our events, a changed time means the user moved it in
     Google Calendar: that block is locked at the new time and the rest is replanned around it. A deleted event is
     recreated (the plan still wants it).
  4. reconcile(): hourly, list our tagged events in Google and repair drift: recreate missing ones, delete
     orphans and duplicates.

Guarantees: only the task calendar is written, and only if it is writable (read-only calendars are never
touched); past blocks are never moved or deleted; confidential tasks are written as "Focus block", private.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BlockEventLink, Calendar, GoogleAccount, ScheduledBlock, Task, User
from .google import GoogleClient, GoogleError, TokenRevoked

log = logging.getLogger(__name__)
APP_PROP = "meridianApp"
BLOCK_KEY = "meridianBlockId"  # same key calendar_sync uses to recognise our events
TASK_KEY = "meridianTaskId"
MASKED_TITLE = "Focus block"
MAX_BACKOFF = timedelta(hours=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def event_id_for(link_id: uuid.UUID) -> str:
    # Google event ids use base32hex characters (a-v, 0-9), 5-1024 long; hex is a subset.
    return "mrdn" + link_id.hex


def target_calendar(db: Session, user: User) -> Calendar | None:
    cal = db.scalars(select(Calendar).where(Calendar.user_id == user.id, Calendar.is_task_target.is_(True))).first()
    if cal is None or not cal.writable:
        return None
    return cal


# -- 1. desired state -------------------------------------------------------------------------------------------


def plan_writes(db: Session, user: User, now: datetime | None = None) -> dict:
    """Stage writes for the current plan. Returns counts of what changed. Caller commits."""
    from .scheduling import current_run, extra_setting

    now = now or now_utc()
    target = target_calendar(db, user)
    links = list(db.scalars(select(BlockEventLink).where(BlockEventLink.user_id == user.id, BlockEventLink.state != "archived")))

    # Blocks that have finished stay in Google as a record of the work; they're never moved again.
    for link in links:
        if link.written_end and link.written_end <= now and link.state in ("synced", "error", "pending"):
            link.state = "archived"
    links = [ln for ln in links if ln.state != "archived"]

    desired: dict[tuple[uuid.UUID, int], tuple[datetime, datetime, str, bool]] = {}
    dest: dict[uuid.UUID, uuid.UUID] = {}  # task -> calendar its blocks go to
    writable = {c.id for c in db.scalars(select(Calendar).where(Calendar.user_id == user.id)) if c.writable}
    if target is not None:
        run = current_run(db, user)
        horizon = now + timedelta(days=int(extra_setting(user, "write_days")))
        blocks = [b for b in (run.blocks if run else []) if b.end_at > now and b.start_at < horizon]
        tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_({b.task_id for b in blocks})))} if blocks else {}
        by_task: dict[uuid.UUID, list[ScheduledBlock]] = {}
        for b in blocks:
            t = tasks.get(b.task_id)
            if t is None or t.deleted_at is not None or t.status in ("done", "cancelled"):
                continue
            by_task.setdefault(b.task_id, []).append(b)
        for tid, bl in by_task.items():
            t = tasks[tid]
            # A task may name its own (writable) calendar; otherwise the default task calendar.
            dest[tid] = t.calendar_id if t.calendar_id in writable else target.id
            for slot, b in enumerate(sorted(bl, key=lambda x: x.start_at), 1):
                title = MASKED_TITLE if t.confidential else t.title
                desired[(tid, slot)] = (b.start_at, b.end_at, title, bool(t.confidential))

    counts = {"create": 0, "update": 0, "delete": 0}
    for link in links:
        key = (link.task_id, link.slot)
        if key in desired and dest.get(link.task_id) == link.calendar_id and link.state != "delete":
            s, e, title, private = desired.pop(key)
            changed = (s, e, title, private) != (link.desired_start, link.desired_end, link.desired_title, link.desired_private)
            link.desired_start, link.desired_end, link.desired_title, link.desired_private = s, e, title, private
            if (s, e, title) != (link.written_start, link.written_end, link.written_title):
                if link.state != "pending" or changed:
                    link.state, link.attempts, link.next_attempt_at = "pending", 0, None
                    counts["update"] += 1
            elif link.state == "pending":
                link.state = "synced"
        elif link.written_start is None:
            db.delete(link)  # never reached Google; nothing to remove
        elif link.state != "delete":
            link.state, link.attempts, link.next_attempt_at = "delete", 0, None
            counts["delete"] += 1
    for (tid, slot), (s, e, title, private) in desired.items():
        lid = uuid.uuid4()
        db.add(BlockEventLink(
            id=lid, user_id=user.id, calendar_id=dest[tid], task_id=tid, slot=slot, google_event_id=event_id_for(lid),
            desired_start=s, desired_end=e, desired_title=title, desired_private=private, state="pending",
        ))
        counts["create"] += 1
    db.flush()
    return counts


# -- 2. sending -------------------------------------------------------------------------------------------------


def _body(link: BlockEventLink, tz: str, with_id: bool) -> dict:
    body = {
        "summary": link.desired_title,
        "description": "Planned by Elevate." + ("" if link.desired_private else " Move this event to lock the task at the new time."),
        "start": {"dateTime": link.desired_start.isoformat(), "timeZone": tz},
        "end": {"dateTime": link.desired_end.isoformat(), "timeZone": tz},
        "transparency": "opaque",
        "visibility": "private" if link.desired_private else "default",
        "reminders": {"useDefault": False},
        "status": "confirmed",
        "extendedProperties": {"private": {APP_PROP: "1", BLOCK_KEY: str(link.id), TASK_KEY: str(link.task_id)}},
    }
    if with_id:
        body["id"] = link.google_event_id
    return body


def _backoff(attempts: int) -> timedelta:
    return min(MAX_BACKOFF, timedelta(seconds=30 * (2 ** max(0, attempts - 1))))


def pending_count(db: Session, user: User) -> int:
    return len(db.scalars(select(BlockEventLink.id).where(BlockEventLink.user_id == user.id, BlockEventLink.state.in_(("pending", "delete")))).all())


def flush(db: Session, user: User, client: GoogleClient, budget: int = 50, now: datetime | None = None) -> int:
    """Send up to `budget` pending writes. Returns how many requests were made."""
    from .calendar_sync import access_token_for

    now = now or now_utc()
    rows = db.scalars(
        select(BlockEventLink)
        .where(
            BlockEventLink.user_id == user.id,
            BlockEventLink.state.in_(("pending", "delete")),
            (BlockEventLink.next_attempt_at.is_(None)) | (BlockEventLink.next_attempt_at <= now),
        )
        .order_by(BlockEventLink.updated_at)
        .limit(budget)
    ).all()
    sent = 0
    tokens: dict[uuid.UUID, str | None] = {}
    for link in rows:
        cal = db.get(Calendar, link.calendar_id)
        if cal is None:
            db.delete(link)
            continue
        acct = db.get(GoogleAccount, cal.google_account_id)
        if acct is None or acct.status == "needs_reconnect":
            continue
        if acct.id not in tokens:
            try:
                tokens[acct.id] = access_token_for(db, acct, client)
            except TokenRevoked as e:
                acct.status, acct.last_error = "needs_reconnect", e.message
                tokens[acct.id] = None
                db.commit()
            except Exception:
                log.exception("token refresh failed for %s", acct.id)
                tokens[acct.id] = None
        token = tokens[acct.id]
        if not token:
            continue
        try:
            if link.state == "delete":
                if cal.writable:
                    sent += 1
                    client.delete_event(token, cal.google_calendar_id, link.google_event_id)
                db.delete(link)
            else:
                if not cal.writable:
                    # Never write to a read-only calendar, whatever happened to its access role.
                    link.state, link.last_error = "error", "This calendar is read-only now; choose another task calendar"
                    db.commit()
                    continue
                sent += 1
                ev = _write(client, token, cal.google_calendar_id, link, user.timezone)
                link.written_start, link.written_end, link.written_title = link.desired_start, link.desired_end, link.desired_title
                link.etag = ev.get("etag")
                link.state, link.attempts, link.next_attempt_at, link.last_error = "synced", 0, None, None
            db.commit()
        except GoogleError as e:
            db.rollback()
            link = db.get(BlockEventLink, link.id)
            if link is None:
                continue
            link.attempts += 1
            link.last_error = e.message[:500]
            link.next_attempt_at = now + _backoff(link.attempts)
            if e.status in (400, 403) and "rateLimit" not in e.message and "quota" not in e.message.lower():
                link.state = "error"  # not retryable as is; the next plan change retries it
            db.commit()
    return sent


def _write(client: GoogleClient, token: str, cal_id: str, link: BlockEventLink, tz: str) -> dict:
    if link.written_start is None:
        try:
            return client.insert_event(token, cal_id, _body(link, tz, with_id=True))
        except GoogleError as e:
            if e.status != 409:
                raise
            # Already exists (an earlier attempt reached Google): update it instead.
            return client.patch_event(token, cal_id, link.google_event_id, _body(link, tz, with_id=False))
    try:
        return client.patch_event(token, cal_id, link.google_event_id, _body(link, tz, with_id=False))
    except GoogleError as e:
        if e.status not in (404, 410):
            raise
        return client.insert_event(token, cal_id, _body(link, tz, with_id=True))


# -- 3. reading our events back -----------------------------------------------------------------------------------


def _parse_dt(obj: dict | None) -> datetime | None:
    if not obj or "dateTime" not in obj:
        return None
    dt = datetime.fromisoformat(obj["dateTime"])
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def on_synced_block(db: Session, cal: Calendar, item: dict) -> str | None:
    """Calendar sync saw one of our events. Returns "moved", "restored" or None."""
    priv = (item.get("extendedProperties") or {}).get("private") or {}
    link = db.scalars(select(BlockEventLink).where(BlockEventLink.calendar_id == cal.id, BlockEventLink.google_event_id == item.get("id"))).first()
    if link is None or link.state in ("archived", "delete"):
        return None
    if priv and priv.get(BLOCK_KEY) not in (None, str(link.id)):
        return None
    now = now_utc()
    if item.get("status") == "cancelled":
        if link.written_end and link.written_end > now and link.state == "synced":
            link.state, link.attempts, link.next_attempt_at = "pending", 0, None
            link.written_title = None  # force a write; the patch restores the event
            return "restored"
        return None
    s, e = _parse_dt(item.get("start")), _parse_dt(item.get("end"))
    if s is None or e is None or link.written_start is None or link.state != "synced":
        return None
    if (s, e) == (link.written_start, link.written_end):
        return None
    _lock_moved_block(db, link, s, e)
    return "moved"


def _lock_moved_block(db: Session, link: BlockEventLink, start: datetime, end: datetime) -> None:
    """The user dragged a task event in Google Calendar: lock that session at its new time."""
    from .scheduling import current_run, mark_dirty

    user = db.get(User, link.user_id)
    if user is None:
        return
    run = current_run(db, user)
    blocks = sorted([b for b in (run.blocks if run else []) if b.task_id == link.task_id], key=lambda b: abs((b.start_at - link.written_start).total_seconds()))
    if blocks:
        b = blocks[0]
        b.start_at, b.end_at, b.locked = start, end, True
        note = "Moved by you in Google Calendar, so it's locked there"
        b.reason = {**(b.reason or {}), "why": [note], "bound_by": "locked", "lock_note": note}
    link.written_start = link.desired_start = start
    link.written_end = link.desired_end = end
    t = db.get(Task, link.task_id)
    mark_dirty(user, f"\"{t.title if t else 'task'}\" moved in Google Calendar")


# -- 4. reconciliation ------------------------------------------------------------------------------------------


def reconcile(db: Session, user: User, client: GoogleClient, now: datetime | None = None) -> dict:
    """Compare our tagged events in Google with the links and repair any drift."""
    from .calendar_sync import access_token_for
    from .scheduling import extra_setting

    now = now or now_utc()
    out = {"recreated": 0, "orphans_deleted": 0}
    cals = {c.id: c for c in [target_calendar(db, user)] if c is not None}
    for cid in set(db.scalars(select(BlockEventLink.calendar_id).where(BlockEventLink.user_id == user.id, BlockEventLink.state != "archived"))):
        c = db.get(Calendar, cid)
        if c is not None and c.writable:
            cals[c.id] = c
    for cal in cals.values():
        res = _reconcile_calendar(db, user, client, cal, now, access_token_for, extra_setting)
        out = {k: out[k] + res[k] for k in out}
    db.commit()
    return out


def _reconcile_calendar(db, user, client, cal, now, access_token_for, extra_setting) -> dict:
    out = {"recreated": 0, "orphans_deleted": 0}
    acct = db.get(GoogleAccount, cal.google_account_id)
    if acct is None or acct.status == "needs_reconnect":
        return out
    token = access_token_for(db, acct, client)
    horizon = now + timedelta(days=int(extra_setting(user, "write_days")) + 1)
    items = client.list_tagged_events(token, cal.google_calendar_id, f"{APP_PROP}=1", now - timedelta(days=1), horizon)
    links = {ln.google_event_id: ln for ln in db.scalars(select(BlockEventLink).where(BlockEventLink.calendar_id == cal.id))}
    seen = set()
    for it in items:
        if it.get("status") == "cancelled":
            continue
        seen.add(it["id"])
        link = links.get(it["id"])
        end = _parse_dt(it.get("end"))
        if link is None or link.state == "delete":
            # Ours but unknown (a duplicate, or left over from a lost write): remove it if it's in the future.
            if end is None or end > now:
                client.delete_event(token, cal.google_calendar_id, it["id"])
                out["orphans_deleted"] += 1
                if link is not None:
                    db.delete(link)
    for eid, link in links.items():
        if link.state == "synced" and link.written_end and link.written_end > now and link.written_start and link.written_start < horizon and eid not in seen:
            link.state, link.attempts, link.next_attempt_at = "pending", 0, None
            link.written_title = None
            out["recreated"] += 1
    return out


def write_status(db: Session, user: User) -> dict:
    cal = target_calendar(db, user)
    rows = db.scalars(select(BlockEventLink).where(BlockEventLink.user_id == user.id, BlockEventLink.state != "archived")).all()
    errors = [r.last_error for r in rows if r.state == "error" or (r.attempts and r.last_error)]
    return {
        "calendar_id": str(cal.id) if cal else None,
        "calendar": cal.summary if cal else None,
        "synced": sum(1 for r in rows if r.state == "synced"),
        "pending": sum(1 for r in rows if r.state in ("pending", "delete")),
        "errors": len(errors),
        "last_error": errors[0] if errors else None,
    }
