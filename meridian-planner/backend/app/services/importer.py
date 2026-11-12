"""CSV import (Motion's task export or any similar CSV), with a review step before anything is saved.

Flow: upload -> parse() guesses which column is which and reads every row -> the user reviews, fixes the column
mapping and picks rows -> apply() creates the tasks through the normal task service.

Duplicates are caught three ways:
- already imported: every imported task keeps an external_ref ("motion:<id>" when the file has an ID column,
  otherwise a hash of title, deadline and project), so importing the same file again creates nothing;
- already in Meridian: an open task with the same title and the same deadline day;
- repeated in the file: the same title, deadline and project twice.
"""

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Category, ImportJob, Project, Task, User
from ..schemas import TaskCreate
from . import scheduling
from . import tasks as svc

MAX_BYTES = 5_000_000
MAX_ROWS = 5000
DEFAULT_DUE = time(17, 0)  # same as quick add: a date with no time means 5 pm local

TARGETS: list[tuple[str, str]] = [
    ("title", "Title"),
    ("description", "Description"),
    ("status", "Status"),
    ("completed_flag", "Completed (yes/no)"),
    ("priority", "Priority"),
    ("deadline", "Deadline"),
    ("deadline_type", "Deadline type (hard/soft)"),
    ("hard_flag", "Hard deadline (yes/no)"),
    ("duration", "Duration"),
    ("earliest_start", "Start date"),
    ("project", "Project"),
    ("category", "Category or workspace"),
    ("tags", "Labels or tags"),
    ("completed_at", "Completed at"),
    ("external_id", "ID"),
    ("notes", "Keep in notes"),
    ("ignore", "Ignore"),
]
TARGET_KEYS = {k for k, _ in TARGETS}

# Normalised header (lowercase letters and digits only) -> target.
ALIASES = {
    "name": "title", "title": "title", "task": "title", "taskname": "title", "tasktitle": "title",
    "description": "description", "details": "description", "notes": "description", "note": "description",
    "status": "status", "state": "status",
    "completed": "completed_flag", "done": "completed_flag", "iscompleted": "completed_flag",
    "priority": "priority",
    "duedate": "deadline", "deadline": "deadline", "due": "deadline", "dueat": "deadline", "duedatetime": "deadline",
    "deadlinetype": "deadline_type",
    "harddeadline": "hard_flag", "ishard": "hard_flag", "isharddeadline": "hard_flag",
    "duration": "duration", "durationminutes": "duration", "durationmins": "duration", "durationmin": "duration",
    "estimatedduration": "duration", "estimate": "duration", "timeestimate": "duration", "expectedduration": "duration",
    "startdate": "earliest_start", "starton": "earliest_start", "startat": "earliest_start", "earlieststart": "earliest_start",
    "project": "project", "projectname": "project",
    "workspace": "category", "workspacename": "category", "category": "category", "area": "category",
    "labels": "tags", "label": "tags", "tags": "tags",
    "completedat": "completed_at", "completedtime": "completed_at", "completeddate": "completed_at",
    "datecompleted": "completed_at", "completedon": "completed_at",
    "id": "external_id", "taskid": "external_id",
    "assignee": "notes", "assignees": "notes", "scheduledstart": "notes", "scheduledend": "notes",
    "recurring": "notes", "recurrence": "notes", "repeat": "notes", "createdat": "ignore", "createdtime": "ignore",
    "updatedat": "ignore", "creator": "ignore", "createdby": "ignore",
}

PRIORITY = {
    "asap": "urgent", "urgent": "urgent", "critical": "urgent", "highest": "urgent", "p0": "urgent",
    "high": "high", "p1": "high",
    "medium": "medium", "normal": "medium", "p2": "medium", "": "medium", "none": "medium",
    "low": "low", "lowest": "low", "p3": "low", "p4": "low",
}
STATUS = {
    "completed": "done", "complete": "done", "done": "done", "closed": "done", "finished": "done",
    "canceled": "cancelled", "cancelled": "cancelled", "archived": "cancelled",
    "inprogress": "in_progress", "started": "in_progress", "doing": "in_progress", "active": "in_progress",
    "blocked": "blocked", "waiting": "blocked", "onhold": "blocked",
    "todo": "todo", "notstarted": "todo", "open": "todo", "backlog": "todo", "": "todo", "new": "todo",
}
TRUE = {"true", "yes", "y", "1", "x", "checked", "done", "completed", "hard"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_title(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# -- reading --------------------------------------------------------------------------------------------------------


def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("The file is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("The file has no rows")
    header = [h.strip() for h in rows[0]]
    if len(rows) - 1 > MAX_ROWS:
        raise ValueError(f"The file has {len(rows) - 1} rows; the limit is {MAX_ROWS}")
    return header, rows[1:]


def guess_mapping(header: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for h in header:
        t = ALIASES.get(norm(h), "notes")
        # Only one column per target, except notes and ignore.
        if t in used and t not in ("notes", "ignore"):
            t = "notes"
        used.add(t)
        out[h] = t
    return out


# -- value parsers ----------------------------------------------------------------------------------------------


def parse_duration(v: str) -> int | None:
    s = (v or "").strip().lower()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return round(float(s))  # bare numbers are minutes
    m = re.fullmatch(r"(\d+):(\d{1,2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b", s):
        found = True
        n = float(num)
        if unit.startswith("d"):
            total += n * 8 * 60  # a working day
        elif unit.startswith("h"):
            total += n * 60
        else:
            total += n
    return round(total) if found else None


def _has_time(s: str) -> bool:
    return bool(re.search(r"\d:\d|\d\s*(am|pm)\b|T\d", s, re.I))


def parse_when(v: str, tz: ZoneInfo, date_only_time: time, dayfirst: bool) -> datetime | None:
    s = (v or "").strip()
    if not s:
        return None
    try:
        d = dtparser.parse(s, dayfirst=dayfirst)
    except (ValueError, OverflowError):
        raise ValueError(f"can't read the date “{s}”")
    if not _has_time(s):
        d = datetime.combine(d.date(), date_only_time)
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(timezone.utc)


def truthy(v: str) -> bool:
    return (v or "").strip().lower() in TRUE


# -- rows ------------------------------------------------------------------------------------------------------------


@dataclass
class Row:
    index: int
    raw: dict
    fields: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    state: str = "new"  # new | duplicate | duplicate_in_file | already_imported | completed | error
    duplicate_of: dict | None = None
    external_ref: str | None = None
    action: str = "import"


def _row_fields(raw: dict, mapping: dict, tz: ZoneInfo, opts: dict) -> tuple[dict, list[str], str | None]:
    f: dict = {"tags": [], "notes": []}
    warns: list[str] = []
    dayfirst = opts.get("date_order") == "dmy"
    by_target: dict[str, str] = {}
    for col, target in mapping.items():
        val = (raw.get(col) or "").strip()
        if target == "notes":
            if val:
                f["notes"].append(f"{col}: {val}")
        elif target != "ignore" and target in TARGET_KEYS:
            by_target[target] = val

    title = by_target.get("title", "")
    if not title:
        return f, warns, "No title"
    f["title"] = title[:500]
    if by_target.get("description"):
        f["description"] = by_target["description"]

    pr = norm(by_target.get("priority", ""))
    if pr in PRIORITY:
        f["priority"] = PRIORITY[pr]
    else:
        f["priority"] = "medium"
        warns.append(f"Unknown priority “{by_target['priority']}”, using medium")

    st = norm(by_target.get("status", ""))
    status = STATUS.get(st)
    if status is None:
        status = "todo"
        warns.append(f"Unknown status “{by_target['status']}”, using To do")
    if truthy(by_target.get("completed_flag", "")):
        status = "done"
    f["status"] = status

    for key, t in (("deadline", DEFAULT_DUE), ("earliest_start", time(0, 0)), ("completed_at", time(12, 0))):
        try:
            f[key] = parse_when(by_target.get(key, ""), tz, t, dayfirst)
        except ValueError as e:
            f[key] = None
            warns.append(f"{dict(TARGETS)[key]}: {e}")
    dt = norm(by_target.get("deadline_type", ""))
    if dt in ("hard", "soft"):
        f["deadline_type"] = dt
    elif "hard_flag" in by_target:
        f["deadline_type"] = "hard" if truthy(by_target["hard_flag"]) else "soft"
    else:
        f["deadline_type"] = opts.get("deadline_type", "soft")

    dur = parse_duration(by_target.get("duration", ""))
    if dur is None:
        dur = int(opts.get("default_duration", 30))
        if by_target.get("duration"):
            warns.append(f"Can't read the duration “{by_target['duration']}”, using {dur} min")
        else:
            warns.append(f"No duration, using {dur} min")
    elif dur < 5:
        # Motion reminders have no duration; give them the smallest block.
        dur = 5
    f["duration_min"] = min(dur, 60 * 200)

    if by_target.get("project"):
        f["project"] = by_target["project"][:200]
    if by_target.get("category"):
        f["category"] = by_target["category"][:100]
    if by_target.get("tags"):
        f["tags"] = [t.strip() for t in re.split(r"[;,|]", by_target["tags"]) if t.strip()]
    if by_target.get("external_id"):
        f["external_id"] = by_target["external_id"][:150]
    return f, warns, None


def _ref(source: str, f: dict) -> str:
    if f.get("external_id"):
        prefix = "motion" if source == "motion_csv" else "csv"
        return f"{prefix}:{f['external_id']}"
    key = "|".join([norm_title(f.get("title", "")), f["deadline"].date().isoformat() if f.get("deadline") else "", (f.get("project") or "").lower()])
    return "csv:" + hashlib.sha1(key.encode()).hexdigest()[:20]


def analyse(db: Session, user: User, job: ImportJob) -> dict:
    """Parse the job's file with its mapping and options; classify every row. Nothing is saved."""
    header, data = read_csv(job.raw or "")
    mapping = {h: job.mapping.get(h, "notes") for h in header}
    opts = job.options or {}
    tz = ZoneInfo(user.timezone)

    existing = db.execute(
        select(Task.id, Task.title, Task.deadline, Task.external_ref, Task.deleted_at, Task.status).where(Task.user_id == user.id)
    ).all()
    by_ref = {r.external_ref: r for r in existing if r.external_ref}
    by_key: dict[tuple, object] = {}
    for r in existing:
        if r.deleted_at is None:
            by_key.setdefault((norm_title(r.title), r.deadline.astimezone(tz).date() if r.deadline else None), r)

    rows: list[Row] = []
    seen: dict[tuple, int] = {}
    for i, cells in enumerate(data):
        raw = {h: (cells[j] if j < len(cells) else "") for j, h in enumerate(header)}
        f, warns, err = _row_fields(raw, mapping, tz, opts)
        row = Row(index=i, raw=raw, fields=f, warnings=warns, error=err)
        if err:
            row.state, row.action = "error", "skip"
            rows.append(row)
            continue
        row.external_ref = _ref(job.source, f)
        key = (norm_title(f["title"]), f["deadline"].astimezone(tz).date() if f.get("deadline") else None)
        file_key = key + ((f.get("project") or "").lower(),)
        if row.external_ref in by_ref:
            m = by_ref[row.external_ref]
            row.state, row.duplicate_of = "already_imported", {"id": str(m.id), "title": m.title, "deleted": m.deleted_at is not None}
        elif key in by_key:
            m = by_key[key]
            row.state, row.duplicate_of = "duplicate", {"id": str(m.id), "title": m.title, "deleted": False}
        elif file_key in seen:
            row.state, row.duplicate_of = "duplicate_in_file", {"row": seen[file_key]}
        elif f["status"] in ("done", "cancelled"):
            row.state = "completed"
        seen.setdefault(file_key, i)
        default = "import" if row.state == "new" or (row.state == "completed" and opts.get("include_completed")) else "skip"
        row.action = job.decisions.get(str(i), default)
        rows.append(row)

    cats = {c.name.strip().lower(): c for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    projs = {p.name.strip().lower() for p in db.scalars(select(Project).where(Project.user_id == user.id))}
    chosen = [r for r in rows if r.action == "import" and not r.error]
    new_projects = sorted({r.fields["project"] for r in chosen if r.fields.get("project") and r.fields["project"].strip().lower() not in projs})
    unknown_cats = sorted({r.fields["category"] for r in chosen if r.fields.get("category") and r.fields["category"].strip().lower() not in cats})
    count = lambda s: sum(1 for r in rows if r.state == s)  # noqa: E731
    return {
        "header": header,
        "mapping": mapping,
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "to_import": len(chosen),
            "new": count("new"),
            "duplicates": count("duplicate") + count("duplicate_in_file"),
            "already_imported": count("already_imported"),
            "completed": count("completed"),
            "errors": count("error"),
            "new_projects": new_projects,
            "unknown_categories": unknown_cats,
        },
    }


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def view(db: Session, user: User, job: ImportJob) -> dict:
    out = {
        "id": str(job.id),
        "source": job.source,
        "filename": job.filename,
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "applied_at": job.applied_at.isoformat() if job.applied_at else None,
        "options": job.options,
        "result": job.result,
        "targets": [{"key": k, "label": label} for k, label in TARGETS],
    }
    if job.status != "review":
        return out
    a = analyse(db, user, job)
    samples = {}
    for h in a["header"]:
        samples[h] = next((str(r.raw.get(h))[:60] for r in a["rows"] if (r.raw.get(h) or "").strip()), "")
    out |= {
        "header": a["header"],
        "samples": samples,
        "mapping": a["mapping"],
        "summary": a["summary"],
        "rows": [
            {
                "index": r.index,
                "state": r.state,
                "action": r.action,
                "error": r.error,
                "warnings": r.warnings,
                "duplicate_of": r.duplicate_of,
                "fields": {k: _iso(v) for k, v in r.fields.items() if k not in ("notes",)},
                "notes": r.fields.get("notes", []),
            }
            for r in a["rows"]
        ],
    }
    return out


def create_job(db: Session, user: User, text: str, filename: str, source: str = "motion_csv") -> ImportJob:
    if len(text.encode()) > MAX_BYTES:
        raise ValueError(f"The file is larger than {MAX_BYTES // 1_000_000} MB")
    header, _ = read_csv(text)
    job = ImportJob(
        user_id=user.id,
        source=source,
        filename=filename[:300],
        raw=text,
        mapping=guess_mapping(header),
        options={"include_completed": False, "default_duration": 30, "deadline_type": "soft", "date_order": "mdy", "create_categories": False},
        decisions={},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def apply(db: Session, user: User, job: ImportJob) -> dict:
    a = analyse(db, user, job)
    opts = job.options or {}
    cats = {c.name.strip().lower(): c for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    projs = {p.name.strip().lower(): p for p in db.scalars(select(Project).where(Project.user_id == user.id))}
    created = 0
    made_projects: list[str] = []
    made_categories: list[str] = []
    task_ids: list[str] = []
    for r in a["rows"]:
        if r.action != "import" or r.error:
            continue
        f = r.fields
        proj = None
        if f.get("project"):
            proj = projs.get(f["project"].strip().lower())
            if proj is None:
                proj = Project(user_id=user.id, name=f["project"], status="active", priority="medium")
                db.add(proj)
                db.flush()
                projs[f["project"].strip().lower()] = proj
                made_projects.append(f["project"])
        cat = None
        if f.get("category"):
            cat = cats.get(f["category"].strip().lower())
            if cat is None and opts.get("create_categories"):
                cat = Category(user_id=user.id, name=f["category"], color="#64748b", position=len(cats))
                db.add(cat)
                db.flush()
                cats[f["category"].strip().lower()] = cat
                made_categories.append(f["category"])
        done = f["status"] in ("done", "cancelled")
        notes = "\n".join(f.get("notes") or [])
        data = TaskCreate(
            title=f["title"],
            description=f.get("description"),
            notes=(f"Imported from {job.filename or 'CSV'}:\n{notes}" if notes else None),
            status="todo" if done else f["status"],
            priority=f["priority"],
            deadline=f.get("deadline"),
            deadline_type=f["deadline_type"],
            earliest_start=f.get("earliest_start"),
            duration_min=max(5, f["duration_min"]),
            project_id=proj.id if proj else None,
            category_id=cat.id if cat else None,
            tags=f.get("tags") or None,
        )
        task = svc.create_task(db, user, data, actor="user", source="import")
        task.external_ref = r.external_ref
        if done:
            task.status = f["status"]
            task.completed_at = f.get("completed_at") or datetime.now(timezone.utc)
            task.remaining_min = 0
            task.completion_pct = 100
        db.commit()
        created += 1
        task_ids.append(str(task.id))
    job.status = "applied"
    job.applied_at = datetime.now(timezone.utc)
    job.result = {
        "created": created,
        "skipped": len(a["rows"]) - created,
        "projects_created": made_projects,
        "categories_created": made_categories,
        "task_ids": task_ids[:2000],
    }
    job.raw = None  # don't keep the uploaded file once it's imported
    db.add(AuditLog(user_id=user.id, action="import_csv", detail={"import_id": str(job.id), "file": job.filename, "created": created}))
    scheduling.mark_dirty(user, "import")
    db.commit()
    return job.result


def cancel(db: Session, user: User, job: ImportJob) -> None:
    job.status = "cancelled"
    job.raw = None
    db.commit()


def get_job(db: Session, user: User, job_id: uuid.UUID) -> ImportJob | None:
    job = db.get(ImportJob, job_id)
    return job if job and job.user_id == user.id else None
