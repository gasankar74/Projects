"""Notifications: what to tell the user, and through which channel.

Every notification is shown in the app. Browser push and email are extra channels, governed by the user's
settings and throttled:
- push: off during quiet hours, at most `push_per_hour` an hour;
- email: only for urgent kinds (a hard deadline that can't be met, a calendar that stopped syncing), at most one
  per kind every 6 hours and 6 a day; plus an optional daily digest.

Generation is idempotent: each notification has a dedupe key (one per task deadline, per sync outage, per day of
overload), so the worker can scan as often as it likes.

Confidential tasks are named "A confidential task" in push and email; the in-app list shows the real title.
"""

import html
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppState, CalendarEvent, GoogleAccount, Notification, ScheduledBlock, Task, User
from . import mailer, scheduling, webpush

log = logging.getLogger("meridian.notify")

URGENT = {"deadline_missed", "sync_failed"}
KINDS = {
    "deadline_missed": "A hard deadline can't be met",
    "deadline_risk": "A deadline is at risk",
    "overload": "More work than free time",
    "sync_failed": "Calendar sync stopped",
    "block_start": "A planned block is about to start",
}
DEFAULTS = {
    "push": True,
    "email_alerts": True,
    "email_digest": False,
    "digest_hour": 7,
    "quiet_start": "21:00",
    "quiet_end": "07:00",
    "push_per_hour": 6,
    "email_to": "",
    "kinds": {"deadline_missed": True, "deadline_risk": True, "overload": True, "sync_failed": True, "block_start": False},
}
SYNC_GRACE = timedelta(minutes=30)
EMAIL_KIND_GAP = timedelta(hours=6)
EMAILS_PER_DAY = 6
BLOCK_LEAD = timedelta(minutes=5)


def prefs(user: User) -> dict:
    p = dict(DEFAULTS) | dict((user.settings or {}).get("notify") or {})
    p["kinds"] = DEFAULTS["kinds"] | dict(p.get("kinds") or {})
    return p


def update_prefs(db: Session, user: User, patch: dict) -> dict:
    cur = dict((user.settings or {}).get("notify") or {})
    if "kinds" in patch:
        patch = dict(patch, kinds=dict(cur.get("kinds") or {}) | {k: bool(v) for k, v in patch["kinds"].items() if k in KINDS})
    cur |= {k: v for k, v in patch.items() if k in DEFAULTS}
    user.settings = dict(user.settings or {}) | {"notify": cur}
    db.commit()
    return prefs(user)


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(user: User, now: datetime) -> bool:
    p = prefs(user)
    if not p.get("quiet_start") or not p.get("quiet_end"):
        return False
    t = now.astimezone(ZoneInfo(user.timezone)).time()
    a, b = _hm(p["quiet_start"]), _hm(p["quiet_end"])
    return (a <= t < b) if a <= b else (t >= a or t < b)


# -- creating ------------------------------------------------------------------------------------------------------


def create(db: Session, user: User, kind: str, title: str, body: str = "", *, link: str | None = None,
           task_id: uuid.UUID | str | None = None, data: dict | None = None, dedupe_key: str | None = None,
           read: bool = False) -> bool:
    """Insert unless a notification with the same dedupe key exists. Returns True when one was created."""
    stmt = insert(Notification).values(
        id=uuid.uuid4(), user_id=user.id, kind=kind, title=title[:300], body=body, link=link,
        task_id=uuid.UUID(str(task_id)) if task_id else None, data=data or {}, dedupe_key=dedupe_key,
        created_at=datetime.now(timezone.utc), channels={}, read_at=datetime.now(timezone.utc) if read else None,
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(constraint="uq_notifications_dedupe")
    res = db.execute(stmt)
    db.commit()
    return bool(res.rowcount)


def _task_link(task_id) -> str:
    return f"/?task={task_id}"


def scan(db: Session, user: User, now: datetime) -> int:
    """Create notifications for what's true right now. Safe to call repeatedly."""
    made = 0
    p = prefs(user)
    tz = ZoneInfo(user.timezone)
    run = scheduling.current_run(db, user)
    if run:
        ids = [uuid.UUID(w["task_id"]) for w in run.warnings if w.get("task_id")]
        tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(ids)))} if ids else {}
        for w in run.warnings:
            t = tasks.get(uuid.UUID(w["task_id"])) if w.get("task_id") else None
            if not t or t.status in ("done", "cancelled") or t.deleted_at:
                continue
            due = t.deadline.isoformat() if t.deadline else "none"
            if w["kind"] == "cannot_meet_deadline":
                made += create(db, user, "deadline_missed", f"Can't finish “{t.title}” by its deadline", w["message"],
                               link=_task_link(t.id), task_id=t.id, dedupe_key=f"deadline_missed:{t.id}:{due}")
            elif w["kind"] in ("at_risk", "will_miss_soft_deadline"):
                made += create(db, user, "deadline_risk", f"“{t.title}” is at risk", w["message"],
                               link=_task_link(t.id), task_id=t.id, dedupe_key=f"deadline_risk:{t.id}:{due}")
        if run.overload:
            day = now.astimezone(tz).date().isoformat()
            made += create(db, user, "overload", "More work than free time", run.overload.get("message", ""),
                           link="/", dedupe_key=f"overload:{day}", data={"suggested": run.overload.get("defer", [])[:5] if isinstance(run.overload.get("defer"), list) else []})

        if p["kinds"].get("block_start"):
            soon = db.scalars(
                select(ScheduledBlock).where(ScheduledBlock.run_id == run.id, ScheduledBlock.start_at > now, ScheduledBlock.start_at <= now + BLOCK_LEAD)
            ).all()
            for b in soon:
                t = db.get(Task, b.task_id)
                if t and t.status not in ("done", "cancelled"):
                    at = b.start_at.astimezone(tz).strftime("%H:%M")
                    made += create(db, user, "block_start", f"Next: “{t.title}” at {at}", "", link=_task_link(t.id), task_id=t.id,
                                   dedupe_key=f"block_start:{t.id}:{b.start_at.isoformat()}", read=True)

    for acct in db.scalars(select(GoogleAccount).where(GoogleAccount.user_id == user.id, GoogleAccount.status.in_(("error", "needs_reconnect")))):
        since = acct.last_synced_at
        if acct.status == "needs_reconnect":
            made += create(db, user, "sync_failed", f"Reconnect {acct.email}", "Google stopped accepting Elevate's access to this account, "
                           "so its calendars aren't updating. Open Calendars & settings and press Reconnect.",
                           link="/settings", dedupe_key=f"sync_failed:{acct.id}:reconnect:{since.isoformat() if since else 'never'}")
        elif since is None or now - since >= SYNC_GRACE:
            made += create(db, user, "sync_failed", f"Calendar sync has failed for {acct.email}",
                           f"No successful sync since {since.astimezone(tz).strftime('%a %H:%M') if since else 'it was connected'}. "
                           f"Last error: {(acct.last_error or 'unknown')[:200]}. The plan may miss new meetings.",
                           link="/settings", dedupe_key=f"sync_failed:{acct.id}:{since.isoformat() if since else 'never'}")
    return made


# -- delivering ----------------------------------------------------------------------------------------------------


def outside_text(db: Session, n: Notification) -> tuple[str, str]:
    """Title and body safe to send outside the app (push, email): confidential tasks are not named."""
    if n.task_id:
        t = db.get(Task, n.task_id)
        if t and t.confidential:
            title = n.title.replace(f"“{t.title}”", "a confidential task")
            title = title[0].upper() + title[1:]
            return title, ""
    return n.title, n.body


def _email_to(user: User) -> str:
    return prefs(user).get("email_to") or user.email


def _sent_count(db: Session, user: User, channel: str, since: datetime, kind: str | None = None) -> int:
    q = select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.created_at >= since, Notification.channels[channel].astext == "sent"
    )
    if kind:
        q = q.where(Notification.kind == kind)
    return db.scalar(q) or 0


def deliver(db: Session, user: User, now: datetime) -> int:
    """Decide push and email for each new notification. Returns how many were handled."""
    p = prefs(user)
    pending = db.scalars(
        select(Notification).where(Notification.user_id == user.id, Notification.delivered_at.is_(None)).order_by(Notification.created_at)
    ).all()
    for n in pending:
        ch: dict = {}
        if now - n.created_at > timedelta(hours=24):
            n.channels, n.delivered_at = {"push": "expired"}, now
            continue
        wanted = p["kinds"].get(n.kind, True) or n.kind == "test"
        title, body = outside_text(db, n)
        # push
        if not webpush.enabled():
            pass
        elif not (p["push"] and wanted):
            ch["push"] = "off"
        elif in_quiet_hours(user, now) and n.kind != "test":
            ch["push"] = "quiet_hours"
        elif _sent_count(db, user, "push", now - timedelta(hours=1)) >= int(p["push_per_hour"]):
            ch["push"] = "throttled"
        else:
            sent = webpush.send(db, user, {"title": title, "body": body, "url": n.link or "/", "tag": n.kind, "id": str(n.id)})
            ch["push"] = "sent" if sent else "no_browser"
        # email: urgent kinds only (and explicit tests)
        if (n.kind in URGENT or n.kind == "test") and mailer.enabled():
            if not (p["email_alerts"] and wanted):
                ch["email"] = "off"
            elif n.kind != "test" and _sent_count(db, user, "email", now - EMAIL_KIND_GAP, n.kind) > 0:
                ch["email"] = "throttled"
            elif n.kind != "test" and _sent_count(db, user, "email", now - timedelta(days=1)) >= EMAILS_PER_DAY:
                ch["email"] = "throttled"
            else:
                link = (get_settings().frontend_url.rstrip("/") + (n.link or "/"))
                ok = mailer.send(_email_to(user), f"Elevate: {title}", f"{title}\n\n{body}\n\nOpen Elevate: {link}\n")
                ch["email"] = "sent" if ok else "failed"
        n.channels = ch
        n.delivered_at = now
        db.commit()
    return len(pending)


# -- daily digest --------------------------------------------------------------------------------------------------


def _state(db: Session, key: str) -> dict:
    row = db.get(AppState, key)
    return dict(row.value) if row else {}


def _set_state(db: Session, key: str, value: dict) -> None:
    row = db.get(AppState, key)
    if row:
        row.value = value
    else:
        db.add(AppState(key=key, value=value))
    db.commit()


def build_digest(db: Session, user: User, now: datetime) -> dict:
    """Today's plan, the next 3 days' deadlines, what's at risk and unread notifications. Confidential tasks are
    not named (the digest leaves the app by email)."""
    tz = ZoneInfo(user.timezone)
    today = now.astimezone(tz).date()
    start = datetime.combine(today, time(0), tz)
    end = start + timedelta(days=1)
    name = lambda t: "Confidential task" if t.confidential else t.title  # noqa: E731
    run = scheduling.current_run(db, user)
    blocks = []
    if run:
        for b in db.scalars(select(ScheduledBlock).where(ScheduledBlock.run_id == run.id, ScheduledBlock.start_at >= start, ScheduledBlock.start_at < end).order_by(ScheduledBlock.start_at)):
            t = db.get(Task, b.task_id)
            if t:
                blocks.append((b.start_at.astimezone(tz).strftime("%H:%M"), b.end_at.astimezone(tz).strftime("%H:%M"), name(t)))
    events = db.scalar(
        select(func.count()).select_from(CalendarEvent).where(
            CalendarEvent.user_id == user.id, CalendarEvent.start_at >= start, CalendarEvent.start_at < end,
            CalendarEvent.status != "cancelled", CalendarEvent.is_task_block.is_(False),
        )
    ) or 0
    due = db.scalars(
        select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.not_in(("done", "cancelled")),
                           Task.deadline >= start, Task.deadline < start + timedelta(days=3)).order_by(Task.deadline)
    ).all()
    overdue = db.scalar(
        select(func.count()).select_from(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.not_in(("done", "cancelled")), Task.deadline < now)
    ) or 0
    risks = []
    if run:
        ids = {w["task_id"] for w in run.warnings if w.get("task_id") and w["kind"] in ("cannot_meet_deadline", "at_risk", "will_miss_soft_deadline")}
        for t in db.scalars(select(Task).where(Task.id.in_([uuid.UUID(i) for i in ids]))) if ids else []:
            if t.status not in ("done", "cancelled") and not t.deleted_at:
                risks.append(name(t))
    unread = db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))) or 0
    lines = [f"Good morning. Your plan for {today.strftime('%A, %B')} {today.day}:", ""]
    lines += [f"  {a}–{b}  {t}" for a, b, t in blocks] or ["  Nothing planned."]
    lines += ["", f"Calendar events today: {events}"]
    if due:
        lines += ["", "Due in the next 3 days:"] + [f"  {t.deadline.astimezone(tz).strftime('%a %H:%M')}  {name(t)}" for t in due[:10]]
    if risks:
        lines += ["", "At risk:"] + [f"  {r}" for r in risks[:10]]
    if overdue:
        lines += ["", f"Overdue tasks: {overdue}"]
    if unread:
        lines += ["", f"Unread notifications: {unread}"]
    link = get_settings().frontend_url.rstrip("/") + "/today"
    lines += ["", f"Open today's plan: {link}", "", "You get this email because the daily digest is on in Elevate's settings."]
    text = "\n".join(lines)
    body_html = "<pre style=\"font: 14px/1.5 system-ui, sans-serif; white-space: pre-wrap\">" + html.escape(text) + "</pre>"
    return {
        "subject": f"Your day: {len(blocks)} planned blocks, {events} events" + (f", {len(risks)} at risk" if risks else ""),
        "text": text,
        "html": body_html,
        "blocks": len(blocks),
        "events": events,
        "due": len(due),
        "at_risk": len(risks),
        "overdue": overdue,
    }


def maybe_send_digest(db: Session, user: User, now: datetime) -> bool:
    p = prefs(user)
    if not (p["email_digest"] and mailer.enabled()):
        return False
    local = now.astimezone(ZoneInfo(user.timezone))
    if local.hour < int(p["digest_hour"]):
        return False
    key = f"digest:{user.id}"
    if _state(db, key).get("date") == local.date().isoformat():
        return False
    d = build_digest(db, user, now)
    ok = mailer.send(_email_to(user), d["subject"], d["text"], d["html"])
    if ok:
        _set_state(db, key, {"date": local.date().isoformat(), "at": now.isoformat()})
    return ok


def tick(db: Session, now: datetime | None = None) -> None:
    """Called by the worker every minute."""
    now = now or datetime.now(timezone.utc)
    for user in db.scalars(select(User)).all():
        try:
            scan(db, user, now)
            deliver(db, user, now)
            maybe_send_digest(db, user, now)
        except Exception:
            db.rollback()
            log.exception("notifications failed for %s", user.id)


def purge(db: Session, now: datetime) -> None:
    """Drop read notifications after 90 days."""
    db.execute(Notification.__table__.delete().where(Notification.read_at.is_not(None), Notification.created_at < now - timedelta(days=90)))
    db.commit()
