import uuid
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Category, ChecklistItem, Task, TaskDependency, TaskEvent, User
from ..schemas import (
    ChecklistIn,
    ChecklistItemOut,
    ChecklistPatch,
    DependencyIn,
    QuickAddIn,
    QuickAddOut,
    TaskCreate,
    TaskEventOut,
    TaskFields,
    TaskList,
    TaskOut,
)
from ..security import current_user
from ..services import tasks as svc
from ..services.quick_add import parse

router = APIRouter(prefix="/tasks", tags=["tasks"])

PRIORITY_ORDER = case(
    {"urgent": 0, "high": 1, "medium": 2, "low": 3}, value=Task.priority, else_=4
)


@router.get("", response_model=TaskList)
def list_tasks(
    view: Literal["active", "inbox", "history", "overdue", "all"] = "active",
    category_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    series_id: uuid.UUID | None = None,
    priority: list[str] | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    q: str | None = None,
    tag: str | None = None,
    due_before: datetime | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    sort: Literal["smart", "deadline", "priority", "created", "completed", "title", "duration"] = "smart",
    limit: int = Query(default=200, le=1000),
    offset: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    conds = [Task.user_id == user.id, Task.deleted_at.is_(None)]
    if view == "active":
        conds.append(Task.status.notin_(svc.DONE_STATES))
    elif view == "inbox":
        conds.append(Task.status == "inbox")
    elif view == "history":
        conds.append(Task.status.in_(svc.DONE_STATES))
    elif view == "overdue":
        conds += [Task.status.notin_(svc.DONE_STATES), Task.deadline < now]
    if category_id:
        conds.append(Task.category_id == category_id)
    if project_id:
        conds.append(Task.project_id == project_id)
    if series_id:
        conds.append(Task.series_id == series_id)
    if priority:
        conds.append(Task.priority.in_(priority))
    if status:
        conds.append(Task.status.in_(status))
    if q:
        like = f"%{q}%"
        conds.append(or_(Task.title.ilike(like), Task.description.ilike(like), Task.notes.ilike(like)))
    if tag:
        conds.append(Task.tags.any(tag.lower()))
    if due_before:
        conds.append(Task.deadline <= due_before)
    if min_duration is not None:
        conds.append(Task.duration_min >= min_duration)
    if max_duration is not None:
        conds.append(Task.duration_min <= max_duration)

    order = {
        # Phase 1 "smart" order: overdue first, then priority, then nearest deadline. Phase 2 replaces this
        # with the scheduler's dynamic score.
        "smart": [case((and_(Task.deadline < now, Task.status.notin_(svc.DONE_STATES)), 0), else_=1), PRIORITY_ORDER, Task.deadline.asc().nulls_last(), Task.created_at],
        "deadline": [Task.deadline.asc().nulls_last(), PRIORITY_ORDER],
        "priority": [PRIORITY_ORDER, Task.deadline.asc().nulls_last()],
        "created": [Task.created_at.desc()],
        "completed": [Task.completed_at.desc().nulls_last()],
        "title": [Task.title],
        "duration": [Task.duration_min.desc()],
    }[sort if not (view == "history" and sort == "smart") else "completed"]

    total = db.scalar(select(func.count()).select_from(Task).where(*conds)) or 0
    rows = db.scalars(select(Task).where(*conds).order_by(*order, Task.id).limit(limit).offset(offset)).all()
    return TaskList(items=svc.bulk_out(db, list(rows)), total=total)


@router.post("", response_model=TaskOut, status_code=201)
def create(body: TaskCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return svc.to_out(db, svc.create_task(db, user, body))


@router.post("/quick-add", response_model=QuickAddOut)
def quick_add(body: QuickAddIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = datetime.now(ZoneInfo(user.timezone))
    r = parse(body.text, now)
    category_id = None
    if r.category_name:
        category_id = db.scalar(select(Category.id).where(Category.user_id == user.id, Category.name == r.category_name))
    parsed = {
        "title": r.title,
        "priority": r.priority,
        "duration_min": r.duration_min,
        "deadline": r.deadline.isoformat() if r.deadline else None,
        "deadline_type": r.deadline_type,
        "can_split": r.can_split,
        "min_session_min": r.min_session_min,
        "max_session_min": r.max_session_min,
        "recurrence": r.recurrence,
        "category_id": str(category_id) if category_id else None,
        "category_name": r.category_name if category_id else None,
        "tags": r.tags,
        "confidential": r.confidential,
    }
    if body.preview:
        return QuickAddOut(parsed=parsed, understood=r.understood)
    data = TaskCreate(
        title=r.title,
        priority=r.priority,
        duration_min=r.duration_min,
        deadline=r.deadline,
        deadline_type=r.deadline_type,
        can_split=r.can_split,
        min_session_min=r.min_session_min,
        max_session_min=r.max_session_min,
        recurrence=r.recurrence,
        tags=r.tags or None,
        confidential=r.confidential or None,
        # Quick-captured tasks land in the inbox for triage, even when a category was guessed.
        status="inbox",
        category_id=category_id,
    )
    task = svc.create_task(db, user, data, source="quick_add")
    return QuickAddOut(parsed=parsed, understood=r.understood, task=svc.to_out(db, task))


@router.get("/{task_id}", response_model=TaskOut)
def get(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return svc.to_out(db, svc.get_task(db, user, task_id))


@router.patch("/{task_id}", response_model=TaskOut)
def patch(task_id: uuid.UUID, body: TaskFields, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    return svc.to_out(db, svc.update_task(db, user, t, body))


@router.post("/{task_id}/complete", response_model=TaskOut)
def complete(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return svc.to_out(db, svc.complete_task(db, user, svc.get_task(db, user, task_id)))


@router.post("/{task_id}/reopen", response_model=TaskOut)
def reopen(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return svc.to_out(db, svc.reopen_task(db, user, svc.get_task(db, user, task_id)))


@router.delete("/{task_id}", status_code=204)
def delete(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    svc.delete_task(db, user, svc.get_task(db, user, task_id))


@router.get("/{task_id}/history", response_model=list[TaskEventOut])
def history(task_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    return db.scalars(select(TaskEvent).where(TaskEvent.task_id == t.id).order_by(TaskEvent.at.desc())).all()


# ----- checklist -----

@router.post("/{task_id}/checklist", response_model=ChecklistItemOut, status_code=201)
def add_item(task_id: uuid.UUID, body: ChecklistIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    pos = (db.scalar(select(func.max(ChecklistItem.position)).where(ChecklistItem.task_id == t.id)) or 0) + 1
    item = ChecklistItem(task_id=t.id, text=body.text, position=pos)
    db.add(item)
    svc.record(db, t, "checklist_added", {"text": body.text})
    db.commit()
    return item


@router.patch("/{task_id}/checklist/{item_id}", response_model=ChecklistItemOut)
def patch_item(task_id: uuid.UUID, item_id: uuid.UUID, body: ChecklistPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    item = db.get(ChecklistItem, item_id)
    if not item or item.task_id != t.id:
        raise HTTPException(404, "Checklist item not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(item, k, v)
    db.commit()
    return item


@router.delete("/{task_id}/checklist/{item_id}", status_code=204)
def delete_item(task_id: uuid.UUID, item_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    item = db.get(ChecklistItem, item_id)
    if not item or item.task_id != t.id:
        raise HTTPException(404, "Checklist item not found")
    db.delete(item)
    db.commit()


# ----- dependencies -----

@router.post("/{task_id}/dependencies", response_model=TaskOut)
def add_dep(task_id: uuid.UUID, body: DependencyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    svc.add_dependency(db, user, t, body.depends_on_task_id)
    return svc.to_out(db, t)


@router.delete("/{task_id}/dependencies/{other_id}", response_model=TaskOut)
def remove_dep(task_id: uuid.UUID, other_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = svc.get_task(db, user, task_id)
    dep = db.scalar(select(TaskDependency).where(TaskDependency.task_id == t.id, TaskDependency.depends_on_task_id == other_id))
    if dep:
        db.delete(dep)
        svc.record(db, t, "dependency_removed", {"depends_on": str(other_id)})
        db.commit()
    return svc.to_out(db, t)
