"""Export, import (JSON round-trip) and account deletion."""

import csv
import logging
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Category, ChecklistItem, Project, Task, TaskDependency, User
from ..schemas import CategoryOut, ProjectOut, TaskCreate
from ..security import current_user, destroy_session
from ..services import tasks as svc
from ..services.accounts import remove_google_footprint
from ..services.google import GoogleClient
from .auth import google_client

router = APIRouter(tags=["data"])

CSV_FIELDS = [
    "id", "title", "status", "priority", "category", "project", "deadline", "deadline_type", "duration_min",
    "remaining_min", "completion_pct", "earliest_start", "can_split", "min_session_min", "max_session_min",
    "tags", "recurrence", "confidential", "created_at", "completed_at", "description",
]


def _all_tasks(db: Session, user: User) -> list[Task]:
    return list(db.scalars(select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None)).order_by(Task.created_at)))


@router.get("/export/json")
def export_json(user: User = Depends(current_user), db: Session = Depends(get_db)):
    cats = db.scalars(select(Category).where(Category.user_id == user.id)).all()
    projs = db.scalars(select(Project).where(Project.user_id == user.id)).all()
    tasks = svc.bulk_out(db, _all_tasks(db, user))
    body = {
        "format": "meridian-export",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {"email": user.email, "name": user.name, "timezone": user.timezone},
        "categories": [CategoryOut.model_validate(c).model_dump(mode="json") for c in cats],
        "projects": [ProjectOut.model_validate(p).model_dump(mode="json") for p in projs],
        "tasks": [t.model_dump(mode="json") for t in tasks],
    }
    db.add(AuditLog(user_id=user.id, action="export_json", detail={"tasks": len(tasks)}))
    db.commit()
    return Response(
        json.dumps(body, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="elevate-export.json"'},
    )


@router.get("/export/tasks.csv")
def export_csv(user: User = Depends(current_user), db: Session = Depends(get_db)):
    cats = {c.id: c.name for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    projs = {p.id: p.name for p in db.scalars(select(Project).where(Project.user_id == user.id))}
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for t in _all_tasks(db, user):
        w.writerow({
            "id": t.id, "title": t.title, "status": t.status, "priority": t.priority,
            "category": cats.get(t.category_id, ""), "project": projs.get(t.project_id, ""),
            "deadline": t.deadline.isoformat() if t.deadline else "", "deadline_type": t.deadline_type,
            "duration_min": t.duration_min, "remaining_min": t.remaining_min, "completion_pct": t.completion_pct,
            "earliest_start": t.earliest_start.isoformat() if t.earliest_start else "", "can_split": t.can_split,
            "min_session_min": t.min_session_min or "", "max_session_min": t.max_session_min or "",
            "tags": ";".join(t.tags or []), "recurrence": (t.recurrence or {}).get("rrule", ""),
            "confidential": t.confidential, "created_at": t.created_at.isoformat(),
            "completed_at": t.completed_at.isoformat() if t.completed_at else "", "description": t.description or "",
        })
    db.add(AuditLog(user_id=user.id, action="export_csv"))
    db.commit()
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="elevate-tasks.csv"'},
    )


class ImportResult(BaseModel):
    created: int
    skipped_duplicates: int


@router.post("/import/json", response_model=ImportResult)
async def import_json(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Re-import an Elevate JSON export. Tasks whose id already exists (or same title + deadline) are skipped."""
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        raise HTTPException(422, "Not valid JSON")
    if body.get("format") != "meridian-export":
        raise HTTPException(422, "Not an Elevate export file")
    cat_by_name = {c.name: c for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    cat_map = {}
    for c in body.get("categories", []):
        existing = cat_by_name.get(c["name"])
        if not existing:
            existing = Category(user_id=user.id, name=c["name"], color=c.get("color", "#6366f1"), position=c.get("position", 0))
            db.add(existing)
            db.flush()
        cat_map[c["id"]] = existing.id
    proj_by_name = {p.name: p for p in db.scalars(select(Project).where(Project.user_id == user.id))}
    proj_map = {}
    for p in body.get("projects", []):
        existing = proj_by_name.get(p["name"])
        if not existing:
            existing = Project(user_id=user.id, name=p["name"], status=p.get("status", "active"), priority=p.get("priority", "medium"))
            db.add(existing)
            db.flush()
        proj_map[p["id"]] = existing.id
    db.commit()

    existing_ids = {str(i) for i in db.scalars(select(Task.id).where(Task.user_id == user.id))}
    existing_keys = {
        (t.title.strip().lower(), t.deadline.isoformat() if t.deadline else None) for t in _all_tasks(db, user)
    }
    created = skipped = 0
    for t in body.get("tasks", []):
        key = (t["title"].strip().lower(), datetime.fromisoformat(t["deadline"]).isoformat() if t.get("deadline") else None)
        if t.get("id") in existing_ids or key in existing_keys:
            skipped += 1
            continue
        allowed = set(TaskCreate.model_fields) - {"checklist", "category_id", "project_id", "parent_task_id", "calendar_id"}
        data = {k: v for k, v in t.items() if k in allowed and v is not None}
        data["category_id"] = cat_map.get(t.get("category_id"))
        data["project_id"] = proj_map.get(t.get("project_id"))
        data["checklist"] = [c["text"] for c in t.get("checklist", [])]
        task = svc.create_task(db, user, TaskCreate(**data), source="import")
        if t.get("status") == "done" and t.get("completed_at"):
            task.completed_at = datetime.fromisoformat(t["completed_at"])
            db.commit()
        existing_keys.add(key)
        created += 1
    db.add(AuditLog(user_id=user.id, action="import_json", detail={"created": created, "skipped": skipped}))
    db.commit()
    return ImportResult(created=created, skipped_duplicates=skipped)


class DeleteAccount(BaseModel):
    confirm_email: str
    # Remove the task blocks Elevate wrote to Google Calendar, and revoke its Google access.
    clean_google: bool = True


@router.post("/account/delete", status_code=204)
def delete_account(body: DeleteAccount, request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db),
                   client: GoogleClient = Depends(google_client)):
    """Permanently deletes the account and all its data. Requires typing the account email."""
    if body.confirm_email.strip().lower() != user.email:
        raise HTTPException(422, "Email does not match")
    if body.clean_google:
        result = remove_google_footprint(db, user, client)
        logging.getLogger("meridian.accounts").info("account deleted: %s", result)
    destroy_session(db, request, response)
    db.delete(user)
    db.commit()
    response.status_code = 204
    return response
