"""Audit views: the account activity log, the change history of every task, and signed-in sessions."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AuditLog, AuthSession, Task, TaskEvent, User
from ..security import _hash, current_user

router = APIRouter(tags=["activity"])

ACCOUNT_LABELS = {
    "signed_in": "Signed in",
    "signed_out": "Signed out",
    "dev_login": "Signed in (local development login)",
    "session_revoked": "Signed out a session",
    "sessions_revoked": "Signed out every other session",
    "google_connected": "Connected a Google account",
    "google_disconnected": "Disconnected a Google account",
    "calendar_sync": "Synced calendars",
    "export_json": "Exported data (JSON)",
    "export_csv": "Exported tasks (CSV)",
    "import_json": "Imported an Elevate export",
    "import_csv": "Imported tasks from CSV",
    "notification_settings": "Changed notification settings",
    "push_subscribed": "Turned on browser notifications",
    "schedule_settings": "Changed scheduling settings",
    "profile_updated": "Changed profile settings",
}
TASK_LABELS = {
    "created": "Created",
    "updated": "Edited",
    "completed": "Completed",
    "reopened": "Reopened",
    "deleted": "Deleted",
    "restored": "Restored",
}
ACTORS = {"user": "You", "ai": "Assistant", "system": "Elevate", "scheduler": "Scheduler"}


def _summarise_diff(diff: dict) -> str:
    keys = [k for k in (diff or {}) if k not in ("updated_at",)]
    if not keys:
        return ""
    shown = ", ".join(k.replace("_", " ") for k in keys[:6])
    return shown + (f" and {len(keys) - 6} more" if len(keys) > 6 else "")


@router.get("/activity")
def activity(
    kind: Literal["all", "account", "tasks", "ai"] = "all",
    before: datetime | None = None,
    limit: int = 50,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Newest first. Page with `before` = the `at` of the last item."""
    limit = min(max(limit, 1), 200)
    items: list[dict] = []
    if kind in ("all", "account"):
        q = select(AuditLog).where(AuditLog.user_id == user.id)
        if before:
            q = q.where(AuditLog.at < before)
        for a in db.scalars(q.order_by(AuditLog.at.desc()).limit(limit)):
            detail = {k: v for k, v in (a.detail or {}).items() if k not in ("token",)}
            items.append({
                "id": f"a:{a.id}", "at": a.at.isoformat(), "source": "account", "actor": "You", "action": a.action,
                "label": ACCOUNT_LABELS.get(a.action, a.action.replace("_", " ").capitalize()), "detail": detail,
                "task_id": None, "task_title": None,
            })
    if kind in ("all", "tasks", "ai"):
        q = select(TaskEvent, Task.title, Task.confidential).join(Task, Task.id == TaskEvent.task_id).where(TaskEvent.user_id == user.id)
        if kind == "ai":
            q = q.where(TaskEvent.actor == "ai")
        if before:
            q = q.where(TaskEvent.at < before)
        for ev, title, _conf in db.execute(q.order_by(TaskEvent.at.desc()).limit(limit)):
            items.append({
                "id": f"t:{ev.id}", "at": ev.at.isoformat(), "source": "task", "actor": ACTORS.get(ev.actor, ev.actor), "action": ev.action,
                "label": TASK_LABELS.get(ev.action, ev.action.replace("_", " ").capitalize()), "detail": {"changed": _summarise_diff(ev.diff)},
                "task_id": str(ev.task_id), "task_title": title,
            })
    items.sort(key=lambda i: i["at"], reverse=True)
    items = items[:limit]
    return {"items": items, "next_before": items[-1]["at"] if len(items) == limit else None}


@router.get("/auth/sessions")
def sessions(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    token = request.cookies.get(get_settings().session_cookie_name) or ""
    mine = _hash(token)
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at > now).order_by(AuthSession.created_at.desc())).all()
    return [
        {"id": str(s.id), "created_at": s.created_at.isoformat(), "expires_at": s.expires_at.isoformat(), "user_agent": s.user_agent, "current": s.token_hash == mine}
        for s in rows
    ]


@router.delete("/auth/sessions/{sid}", status_code=204)
def revoke_session(sid: uuid.UUID, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.get(AuthSession, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "Session not found")
    if s.token_hash == _hash(request.cookies.get(get_settings().session_cookie_name) or ""):
        raise HTTPException(409, "That's this session; use Sign out instead")
    db.delete(s)
    db.add(AuditLog(user_id=user.id, action="session_revoked", detail={"user_agent": s.user_agent[:120]}))
    db.commit()


@router.post("/auth/sessions/revoke-others", status_code=204)
def revoke_others(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    mine = _hash(request.cookies.get(get_settings().session_cookie_name) or "")
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.token_hash != mine))
    db.add(AuditLog(user_id=user.id, action="sessions_revoked"))
    db.commit()
