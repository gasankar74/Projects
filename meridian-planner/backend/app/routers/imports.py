"""Motion / CSV import with a review step."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ImportJob, User
from ..security import current_user
from ..services import importer, motion_api

router = APIRouter(prefix="/imports", tags=["imports"])


class ImportIn(BaseModel):
    filename: str = Field(default="", max_length=300)
    text: str = Field(max_length=importer.MAX_BYTES)
    source: Literal["motion_csv", "csv"] = "motion_csv"


class MotionFetchIn(BaseModel):
    # Used for this request only; never saved or logged.
    api_key: str = Field(min_length=1, max_length=500)
    include_completed: bool = False


def motion_transport():
    """Dependency; tests override it with an httpx.MockTransport."""
    return None


class ImportOptions(BaseModel):
    include_completed: bool | None = None
    default_duration: int | None = Field(default=None, ge=5, le=480)
    deadline_type: Literal["hard", "soft"] | None = None
    date_order: Literal["mdy", "dmy"] | None = None
    create_categories: bool | None = None


class ImportPatch(BaseModel):
    mapping: dict[str, str] | None = None
    options: ImportOptions | None = None
    # Row index -> "import" or "skip". "reset" clears every decision.
    decisions: dict[str, Literal["import", "skip"]] | None = None
    reset_decisions: bool = False


def _job(db: Session, user: User, job_id: uuid.UUID) -> ImportJob:
    job = importer.get_job(db, user, job_id)
    if not job:
        raise HTTPException(404, "Import not found")
    return job


@router.get("")
def list_imports(user: User = Depends(current_user), db: Session = Depends(get_db)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.user_id == user.id).order_by(ImportJob.created_at.desc()).limit(50)).all()
    return [
        {"id": str(j.id), "filename": j.filename, "source": j.source, "status": j.status, "created_at": j.created_at.isoformat(), "result": j.result}
        for j in jobs
    ]


@router.post("", status_code=201)
def upload(body: ImportIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        job = importer.create_job(db, user, body.text, body.filename, body.source)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return importer.view(db, user, job)


@router.post("/motion", status_code=201)
def fetch_from_motion(body: MotionFetchIn, transport=Depends(motion_transport), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Read the user's tasks from Motion's API with their API key and open them in the import review."""
    try:
        text, count = motion_api.fetch_csv(body.api_key, body.include_completed, transport=transport)
        job = importer.create_job(db, user, text, f"Motion ({count} tasks)", "motion_csv")
    except (motion_api.MotionError, ValueError) as e:
        raise HTTPException(422, str(e))
    if body.include_completed:
        job.options = {**job.options, "include_completed": True}
        db.commit()
    return importer.view(db, user, job)


@router.get("/{job_id}")
def get_import(job_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return importer.view(db, user, _job(db, user, job_id))


@router.patch("/{job_id}")
def update_import(job_id: uuid.UUID, body: ImportPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, user, job_id)
    if job.status != "review":
        raise HTTPException(409, "This import is already finished")
    if body.mapping is not None:
        bad = {v for v in body.mapping.values() if v not in importer.TARGET_KEYS}
        if bad:
            raise HTTPException(422, f"Unknown field: {', '.join(sorted(bad))}")
        if list(body.mapping.values()).count("title") > 1:
            raise HTTPException(422, "Only one column can be the title")
        job.mapping = {**job.mapping, **body.mapping}
    if body.options is not None:
        job.options = {**job.options, **body.options.model_dump(exclude_none=True)}
    if body.reset_decisions:
        job.decisions = {}
    if body.decisions:
        job.decisions = {**job.decisions, **body.decisions}
    db.commit()
    return importer.view(db, user, job)


@router.post("/{job_id}/apply")
def apply_import(job_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, user, job_id)
    if job.status != "review":
        raise HTTPException(409, "This import is already finished")
    return importer.apply(db, user, job)


@router.post("/{job_id}/cancel", status_code=204)
def cancel_import(job_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, user, job_id)
    if job.status == "review":
        importer.cancel(db, user, job)
