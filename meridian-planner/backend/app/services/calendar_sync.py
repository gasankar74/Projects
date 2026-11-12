"""Google Calendar sync: calendar list + incremental event sync with recovery.

Guarantees:
- Events are upserted on (calendar_id, google_event_id), so retries and re-syncs never create duplicates.
- A 410 (expired sync token) triggers a full re-sync; events Google no longer returns are removed.
- A revoked refresh token marks the account needs_reconnect; stored events stay so scheduling can continue.
- Read-only calendars are only ever read (this module never writes to Google).
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import AuditLog, Calendar, CalendarEvent, GoogleAccount, User
from ..security import decrypt, encrypt
from .google import GoogleClient, GoogleError, SyncTokenExpired, TokenRevoked

log = logging.getLogger(__name__)
TASK_BLOCK_KEY = "meridianBlockId"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def access_token_for(db: Session, account: GoogleAccount, client: GoogleClient) -> str:
    if account.access_token_enc and account.token_expiry and account.token_expiry > now_utc() + timedelta(seconds=60):
        return decrypt(account.access_token_enc)  # type: ignore[return-value]
    refresh = decrypt(account.refresh_token_enc)
    if not refresh:
        raise TokenRevoked(400, "No refresh token stored; reconnect this account")
    data = client.refresh(refresh)
    account.access_token_enc = encrypt(data["access_token"])
    account.token_expiry = now_utc() + timedelta(seconds=int(data.get("expires_in", 3600)))
    if data.get("refresh_token"):
        account.refresh_token_enc = encrypt(data["refresh_token"])
    db.commit()
    return data["access_token"]


def sync_calendar_list(db: Session, account: GoogleAccount, client: GoogleClient, token: str) -> list[Calendar]:
    items = client.list_calendars(token)
    existing = {c.google_calendar_id: c for c in account.calendars}
    seen = set()
    for it in items:
        gid = it["id"]
        seen.add(gid)
        cal = existing.get(gid)
        if cal is None:
            role = it.get("accessRole", "reader")
            cal = Calendar(
                user_id=account.user_id,
                google_account_id=account.id,
                google_calendar_id=gid,
                access_role=role,
                # Sensible defaults: show everything; free/busy-only calendars still block time.
                selected=it.get("selected", True) or it.get("primary", False),
                blocks_time=True,
            )
            db.add(cal)
            account.calendars.append(cal)
        cal.summary = it.get("summaryOverride") or it.get("summary") or gid
        cal.description = it.get("description")
        cal.color = it.get("backgroundColor")
        cal.access_role = it.get("accessRole", cal.access_role)
        cal.time_zone = it.get("timeZone")
        cal.primary = bool(it.get("primary"))
        if not cal.writable:
            cal.is_task_target = False  # never write to read-only calendars
    # Calendars the user no longer has access to are removed with their events.
    for gid, cal in existing.items():
        if gid not in seen:
            db.delete(cal)
    db.commit()
    return [c for c in account.calendars if c.google_calendar_id in seen]


def _parse_time(obj: dict | None, tz_fallback: str) -> tuple[datetime | None, date | None, str | None]:
    if not obj:
        return None, None, None
    if "dateTime" in obj:
        dt = datetime.fromisoformat(obj["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(obj.get("timeZone") or tz_fallback))
        return dt.astimezone(timezone.utc), None, obj.get("timeZone")
    if "date" in obj:
        d = date.fromisoformat(obj["date"])
        tz = ZoneInfo(obj.get("timeZone") or tz_fallback)
        return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc), d, obj.get("timeZone")
    return None, None, None


def _apply_event(ev: CalendarEvent, item: dict, tz_fallback: str) -> None:
    ev.status = item.get("status", "confirmed")
    ev.summary = item.get("summary") or ("Busy" if ev.status != "cancelled" else ev.summary or "")
    ev.location = item.get("location")
    ev.html_link = item.get("htmlLink")
    ev.ical_uid = item.get("iCalUID")
    ev.recurring_event_id = item.get("recurringEventId")
    ev.etag = item.get("etag")
    ev.transparency = item.get("transparency", "opaque")
    ev.visibility = item.get("visibility", "default")
    ev.self_response = next(
        (a.get("responseStatus") for a in item.get("attendees", []) if a.get("self")), None
    )
    priv = (item.get("extendedProperties") or {}).get("private") or {}
    ev.is_task_block = TASK_BLOCK_KEY in priv
    if "start" in item:
        ev.start_at, ev.start_date, ev.time_zone = _parse_time(item.get("start"), tz_fallback)
        ev.end_at, ev.end_date, _ = _parse_time(item.get("end"), tz_fallback)
        ev.all_day = ev.start_date is not None


def sync_calendar_events(db: Session, cal: Calendar, client: GoogleClient, token: str, tz_fallback: str) -> dict:
    full = cal.sync_token is None
    try:
        items, next_token = client.list_events(token, cal.google_calendar_id, cal.sync_token)
    except SyncTokenExpired:
        log.info("sync token expired for calendar %s; running full re-sync", cal.id)
        cal.sync_token = None
        full = True
        items, next_token = client.list_events(token, cal.google_calendar_id, None)

    tz = cal.time_zone or tz_fallback
    existing = {
        e.google_event_id: e
        for e in db.scalars(select(CalendarEvent).where(CalendarEvent.calendar_id == cal.id))
    }
    seen: set[str] = set()
    created = updated = cancelled = 0
    for item in items:
        gid = item["id"]
        seen.add(gid)
        ev = existing.get(gid)
        if item.get("status") == "cancelled":
            if ev is not None and ev.status != "cancelled":
                ev.status = "cancelled"
                cancelled += 1
            continue
        if ev is None:
            ev = CalendarEvent(user_id=cal.user_id, calendar_id=cal.id, google_event_id=gid)
            db.add(ev)
            existing[gid] = ev
            created += 1
        else:
            updated += 1
        _apply_event(ev, item, tz)

    removed = 0
    if full:
        # A full sync is the source of truth: anything not returned no longer exists in range.
        stale = [gid for gid in existing if gid not in seen]
        if stale:
            removed = len(stale)
            db.execute(
                delete(CalendarEvent).where(
                    CalendarEvent.calendar_id == cal.id, CalendarEvent.google_event_id.in_(stale)
                )
            )
    cal.sync_token = next_token
    cal.last_synced_at = now_utc()
    cal.last_error = None
    db.commit()
    return {"created": created, "updated": updated, "cancelled": cancelled, "removed": removed, "full": full}


def sync_account(db: Session, account: GoogleAccount, client: GoogleClient) -> dict:
    user = db.get(User, account.user_id)
    tz_fallback = user.timezone if user else "UTC"
    result: dict = {"account": account.email, "calendars": {}, "ok": True}
    try:
        account.status = "syncing"
        db.commit()
        token = access_token_for(db, account, client)
        cals = sync_calendar_list(db, account, client, token)
        for cal in cals:
            if not cal.selected:
                continue
            try:
                res = sync_calendar_events(db, cal, client, token, tz_fallback)
                result["calendars"][cal.summary] = res
                if user and any(res.get(k) for k in ("created", "updated", "cancelled", "removed")):
                    user.schedule_dirty_at = now_utc()
                    user.schedule_dirty_reason = f"calendar \"{cal.summary}\" changed"
                    db.commit()
            except GoogleError as e:
                db.rollback()
                cal.last_error = e.message[:1000]
                result["calendars"][cal.summary] = {"error": e.message[:200]}
                result["ok"] = False
                db.commit()
        account.status = "ok" if result["ok"] else "error"
        account.last_error = None if result["ok"] else "One or more calendars failed to sync"
        account.last_synced_at = now_utc()
    except TokenRevoked as e:
        db.rollback()
        account.status = "needs_reconnect"
        account.last_error = e.message
        result["ok"] = False
        result["error"] = e.message
    except (GoogleError, Exception) as e:  # network errors etc.: keep last known events, flag the account
        db.rollback()
        log.exception("sync failed for account %s", account.id)
        account.status = "error"
        account.last_error = str(e)[:1000]
        result["ok"] = False
        result["error"] = str(e)[:200]
    db.add(AuditLog(user_id=account.user_id, action="calendar_sync", detail={"account": account.email, "ok": result["ok"]}))
    db.commit()
    return result


def sync_user(db: Session, user: User, client: GoogleClient) -> list[dict]:
    accounts = db.scalars(select(GoogleAccount).where(GoogleAccount.user_id == user.id)).all()
    return [sync_account(db, a, client) for a in accounts]


def busy_intervals(db: Session, user: User, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Merged busy time across every selected, time-blocking calendar (overlaps collapse into one interval)."""
    rows = db.scalars(
        select(CalendarEvent)
        .join(Calendar, Calendar.id == CalendarEvent.calendar_id)
        .where(
            CalendarEvent.user_id == user.id,
            Calendar.selected.is_(True),
            Calendar.blocks_time.is_(True),
            CalendarEvent.start_at < end,
            CalendarEvent.end_at > start,
        )
    ).all()
    spans = sorted((max(e.start_at, start), min(e.end_at, end)) for e in rows if e.is_busy and e.start_at and e.end_at)
    merged: list[list[datetime]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]
