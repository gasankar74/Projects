"""Categories and projects."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Category, Project, SchedulingProfile, Task, User
from ..schemas import CategoryIn, CategoryOut, CategoryPatch, ProjectIn, ProjectOut, ProjectPatch
from ..security import current_user
from ..services.tasks import DONE_STATES

router = APIRouter(tags=["organize"])


def _open_counts(db: Session, user: User, col) -> dict:
    rows = db.execute(
        select(col, func.count())
        .where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status.notin_(DONE_STATES))
        .group_by(col)
    ).all()
    return {k: n for k, n in rows}


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(user: User = Depends(current_user), db: Session = Depends(get_db)):
    counts = _open_counts(db, user, Task.category_id)
    cats = db.scalars(select(Category).where(Category.user_id == user.id).order_by(Category.position, Category.name)).all()
    out = []
    for c in cats:
        o = CategoryOut.model_validate(c)
        o.open_count = counts.get(c.id, 0)
        out.append(o)
    return out


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if db.scalar(select(Category).where(Category.user_id == user.id, Category.name == body.name)):
        raise HTTPException(409, "A category with that name exists")
    _check_profile(db, user, body.scheduling_profile_id)
    c = Category(user_id=user.id, **body.model_dump())
    db.add(c)
    db.commit()
    return c


def _check_profile(db: Session, user: User, pid) -> None:
    if pid is None:
        return
    p = db.get(SchedulingProfile, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(422, "Scheduling profile not found")


def _own_category(db: Session, user: User, cid: uuid.UUID) -> Category:
    c = db.get(Category, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Category not found")
    return c


@router.patch("/categories/{cid}", response_model=CategoryOut)
def patch_category(cid: uuid.UUID, body: CategoryPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = _own_category(db, user, cid)
    fields = body.model_dump(exclude_unset=True)
    if "scheduling_profile_id" in fields:
        _check_profile(db, user, fields["scheduling_profile_id"])
        user.schedule_dirty_at = datetime.now(timezone.utc)
        user.schedule_dirty_reason = f"category \"{c.name}\" hours changed"
    for k, v in fields.items():
        setattr(c, k, v)
    db.commit()
    return c


@router.delete("/categories/{cid}", status_code=204)
def delete_category(cid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Tasks keep existing; their category is cleared (ON DELETE SET NULL).
    db.delete(_own_category(db, user, cid))
    db.commit()


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(include_archived: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Project).where(Project.user_id == user.id)
    if not include_archived:
        q = q.where(Project.status != "archived")
    open_counts = _open_counts(db, user, Task.project_id)
    done_counts = {
        k: n
        for k, n in db.execute(
            select(Task.project_id, func.count())
            .where(Task.user_id == user.id, Task.deleted_at.is_(None), Task.status == "done")
            .group_by(Task.project_id)
        ).all()
    }
    out = []
    for p in db.scalars(q.order_by(Project.name)).all():
        o = ProjectOut.model_validate(p)
        o.open_count = open_counts.get(p.id, 0)
        o.done_count = done_counts.get(p.id, 0)
        out.append(o)
    return out


def _check_category(db: Session, user: User, cid):
    if cid is not None:
        _own_category(db, user, cid)


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_category(db, user, body.category_id)
    p = Project(user_id=user.id, **body.model_dump())
    db.add(p)
    db.commit()
    return p


def _own_project(db: Session, user: User, pid: uuid.UUID) -> Project:
    p = db.get(Project, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Project not found")
    return p


@router.patch("/projects/{pid}", response_model=ProjectOut)
def patch_project(pid: uuid.UUID, body: ProjectPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_project(db, user, pid)
    fields = body.model_dump(exclude_unset=True)
    _check_category(db, user, fields.get("category_id"))
    for k, v in fields.items():
        setattr(p, k, v)
    db.commit()
    return p


@router.delete("/projects/{pid}", status_code=204)
def delete_project(pid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_own_project(db, user, pid))
    db.commit()
