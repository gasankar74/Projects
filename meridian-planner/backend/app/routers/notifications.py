"""In-app notifications, notification settings, browser push subscriptions."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AuditLog, Notification, PushSubscription, User
from ..security import current_user
from ..services import mailer, notify, webpush

router = APIRouter(tags=["notifications"])


def _out(n: Notification) -> dict:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "task_id": str(n.task_id) if n.task_id else None,
        "created_at": n.created_at.isoformat(),
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "channels": n.channels,
    }


@router.get("/notifications")
def list_notifications(unread_only: bool = False, limit: int = 30, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    items = db.scalars(q.order_by(Notification.created_at.desc()).limit(min(max(limit, 1), 200))).all()
    unread = db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))) or 0
    return {"items": [_out(n) for n in items], "unread": unread}


@router.post("/notifications/{nid}/read", status_code=204)
def mark_read(nid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.get(Notification, nid)
    if not n or n.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    n.read_at = n.read_at or datetime.now(timezone.utc)
    db.commit()


@router.post("/notifications/read-all", status_code=204)
def mark_all_read(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None)).values(read_at=datetime.now(timezone.utc)))
    db.commit()


class NotifyPrefs(BaseModel):
    push: bool | None = None
    email_alerts: bool | None = None
    email_digest: bool | None = None
    digest_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$")
    quiet_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$")
    push_per_hour: int | None = Field(default=None, ge=1, le=60)
    email_to: str | None = Field(default=None, max_length=320)
    kinds: dict[str, bool] | None = None


def _settings_out(db: Session, user: User) -> dict:
    s = get_settings()
    subs = db.scalar(select(func.count()).select_from(PushSubscription).where(PushSubscription.user_id == user.id)) or 0
    return {
        "prefs": notify.prefs(user),
        "kinds": notify.KINDS,
        "email": {"enabled": mailer.enabled(), "to": notify.prefs(user).get("email_to") or user.email},
        "push": {"enabled": webpush.enabled(), "public_key": s.vapid_public_key or None, "subscriptions": subs},
    }


@router.get("/notifications/settings")
def get_notify_settings(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _settings_out(db, user)


@router.patch("/notifications/settings")
def patch_notify_settings(body: NotifyPrefs, user: User = Depends(current_user), db: Session = Depends(get_db)):
    patch = body.model_dump(exclude_none=True)
    notify.update_prefs(db, user, patch)
    db.add(AuditLog(user_id=user.id, action="notification_settings", detail={k: v for k, v in patch.items() if k != "email_to"}))
    db.commit()
    return _settings_out(db, user)


@router.post("/notifications/test")
def send_test(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """A test notification through every channel that's on, delivered right away."""
    now = datetime.now(timezone.utc)
    notify.create(db, user, "test", "Test notification", "If you can read this, notifications reach you here.", link="/settings")
    notify.deliver(db, user, now)
    n = db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.kind == "test").order_by(Notification.created_at.desc()).limit(1)).first()
    return _out(n)


@router.get("/notifications/digest/preview")
def digest_preview(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return notify.build_digest(db, user, datetime.now(timezone.utc))


@router.post("/notifications/digest/send")
def digest_send(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not mailer.enabled():
        raise HTTPException(409, "Email isn't set up on the server (MERIDIAN_SMTP_HOST)")
    d = notify.build_digest(db, user, datetime.now(timezone.utc))
    ok = mailer.send(notify.prefs(user).get("email_to") or user.email, d["subject"], d["text"], d["html"])
    if not ok:
        raise HTTPException(502, "Sending the email failed; check the server's SMTP settings")
    return {"sent": True}


class PushKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class PushSub(BaseModel):
    endpoint: str = Field(max_length=2000, pattern=r"^https://")
    keys: PushKeys


class PushUnsub(BaseModel):
    endpoint: str = Field(max_length=2000)


@router.post("/push/subscribe", status_code=204)
def push_subscribe(body: PushSub, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not webpush.enabled():
        raise HTTPException(409, "Browser push isn't set up on the server (MERIDIAN_VAPID_PUBLIC_KEY / PRIVATE_KEY)")
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub and sub.user_id != user.id:
        db.delete(sub)  # the browser now belongs to someone else's session
        db.flush()
        sub = None
    if not sub:
        sub = PushSubscription(user_id=user.id, endpoint=body.endpoint)
        db.add(sub)
        db.add(AuditLog(user_id=user.id, action="push_subscribed", detail={"user_agent": request.headers.get("user-agent", "")[:200]}))
    sub.p256dh, sub.auth = body.keys.p256dh, body.keys.auth
    sub.user_agent = request.headers.get("user-agent", "")[:400]
    sub.failures = 0
    db.commit()


@router.post("/push/unsubscribe", status_code=204)
def push_unsubscribe(body: PushUnsub, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint, PushSubscription.user_id == user.id))
    if sub:
        db.delete(sub)
        db.commit()
