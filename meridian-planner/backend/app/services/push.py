"""Google push notifications (events.watch) with polling as the fallback.

A channel per selected calendar posts to {public_url}/webhooks/google when anything in it changes. The handler
checks the channel token (stored hashed, compared in constant time), marks the calendar for sync and returns at
once; the worker syncs it a few seconds later. Channels are renewed a day before they expire (Google allows
about a week). Without a public URL nothing is registered and the 10-minute poll does the job.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Calendar, GoogleAccount, WatchChannel
from .google import GoogleClient, GoogleError

log = logging.getLogger(__name__)
TTL = timedelta(days=7)
RENEW_BEFORE = timedelta(days=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def webhook_address() -> str | None:
    base = get_settings().public_url.rstrip("/")
    if not base.startswith("https://"):
        return None  # Google only delivers to HTTPS
    return f"{base}/webhooks/google"


def ensure_channels(db: Session, client: GoogleClient, now: datetime | None = None) -> dict:
    """Open channels for selected calendars that have none, renew ones about to expire, close unneeded ones."""
    from .calendar_sync import access_token_for

    now = now or now_utc()
    out = {"opened": 0, "renewed": 0, "closed": 0, "errors": 0}
    address = webhook_address()
    channels = list(db.scalars(select(WatchChannel)))
    by_cal: dict[uuid.UUID, list[WatchChannel]] = {}
    for ch in channels:
        by_cal.setdefault(ch.calendar_id, []).append(ch)
    tokens: dict[uuid.UUID, str] = {}

    def token_for(acct: GoogleAccount) -> str:
        if acct.id not in tokens:
            tokens[acct.id] = access_token_for(db, acct, client)
        return tokens[acct.id]

    for cal in db.scalars(select(Calendar)).all():
        acct = db.get(GoogleAccount, cal.google_account_id)
        mine = by_cal.pop(cal.id, [])
        want = address is not None and cal.selected and acct is not None and acct.status != "needs_reconnect"
        live = [c for c in mine if c.expires_at > now + RENEW_BEFORE]
        try:
            if want and not live:
                token = token_for(acct)
                secret = secrets.token_urlsafe(32)
                cid = str(uuid.uuid4())
                res = client.watch_events(token, cal.google_calendar_id, cid, address, secret, int(TTL.total_seconds()))
                exp_ms = res.get("expiration")
                expires = datetime.fromtimestamp(int(exp_ms) / 1000, timezone.utc) if exp_ms else now + TTL
                db.add(WatchChannel(user_id=cal.user_id, calendar_id=cal.id, channel_id=cid, resource_id=res.get("resourceId"),
                                    token_hash=_hash(secret), expires_at=expires))
                out["renewed" if mine else "opened"] += 1
                live = []
                stale = mine
            else:
                stale = [c for c in mine if c not in live] if want else mine
            for ch in stale:
                if acct is not None and acct.status != "needs_reconnect" and ch.resource_id and ch.expires_at > now:
                    try:
                        client.stop_channel(token_for(acct), ch.channel_id, ch.resource_id)
                    except GoogleError:
                        pass
                db.delete(ch)
                out["closed"] += 1
            db.commit()
        except Exception as e:  # one calendar's failure doesn't stop the others; polling still covers it
            db.rollback()
            out["errors"] += 1
            log.warning("push channel for calendar %s failed: %s", cal.id, e)
    for leftover in by_cal.values():  # channels whose calendar is gone
        for ch in leftover:
            db.delete(ch)
    db.commit()
    return out


def stop_for_account(db: Session, account: GoogleAccount, client: GoogleClient) -> None:
    """Best effort before an account is disconnected."""
    from .calendar_sync import access_token_for

    cal_ids = [c.id for c in account.calendars]
    chans = db.scalars(select(WatchChannel).where(WatchChannel.calendar_id.in_(cal_ids))).all() if cal_ids else []
    if not chans:
        return
    try:
        token = access_token_for(db, account, client)
        for ch in chans:
            if ch.resource_id:
                client.stop_channel(token, ch.channel_id, ch.resource_id)
    except Exception:
        log.info("could not stop push channels for %s; they expire on their own", account.id)


def handle_notification(db: Session, channel_id: str | None, token: str | None, state: str | None) -> bool:
    """Returns True if the notification was genuine. Never does slow work: it only flags the calendar."""
    if not channel_id or not token:
        return False
    ch = db.scalar(select(WatchChannel).where(WatchChannel.channel_id == channel_id))
    if ch is None or not hmac.compare_digest(ch.token_hash, _hash(token)):
        return False
    if state == "sync":
        return True  # the handshake Google sends when a channel opens
    cal = db.get(Calendar, ch.calendar_id)
    if cal is not None and cal.sync_requested_at is None:
        cal.sync_requested_at = now_utc()
        db.commit()
    return True
