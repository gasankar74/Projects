"""Project hierarchy (areas), project detail for the list/Kanban/Gantt views, templates and AI breakdowns."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..ai import privacy
from ..ai.providers import ProviderError, get_provider
from ..db import get_db
from ..models import Area, Category, Project, ProjectTemplate, Task, TaskDependency, User, Workspace
from ..schemas import Priority, ProjectOut, TaskCreate
from ..security import current_user
from ..services import scheduling as sched
from ..services import tasks as svc
from ..services import tracking

router = APIRouter(tags=["projects"])


# -- areas ----------------------------------------------------------------------------------------------------------


class AreaIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    position: int = 0


class AreaPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    position: int | None = None


def default_workspace(db: Session, user: User) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id).order_by(Workspace.created_at).limit(1))
    if ws is None:
        ws = Workspace(user_id=user.id, name="My workspace")
        db.add(ws)
        db.flush()
    return ws


def _area_out(a: Area) -> dict:
    return {"id": str(a.id), "name": a.name, "position": a.position}


@router.get("/areas")
def list_areas(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Area).where(Area.user_id == user.id).order_by(Area.position, Area.name)).all()
    return [_area_out(a) for a in rows]


@router.post("/areas", status_code=201)
def create_area(body: AreaIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ws = default_workspace(db, user)
    a = Area(user_id=user.id, workspace_id=ws.id, name=body.name.strip(), position=body.position)
    db.add(a)
    db.commit()
    return _area_out(a)


def _own_area(db: Session, user: User, aid: uuid.UUID) -> Area:
    a = db.get(Area, aid)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Area not found")
    return a


@router.patch("/areas/{aid}")
def patch_area(aid: uuid.UUID, body: AreaPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = _own_area(db, user, aid)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    db.commit()
    return _area_out(a)


@router.delete("/areas/{aid}", status_code=204)
def delete_area(aid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Projects in it stay; they just have no area (ON DELETE SET NULL).
    db.delete(_own_area(db, user, aid))
    db.commit()


# -- project detail -------------------------------------------------------------------------------------------------


def own_project(db: Session, user: User, pid: uuid.UUID) -> Project:
    p = db.get(Project, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Project not found")
    return p


@router.get("/projects/{pid}")
def project_detail(pid: uuid.UUID, include_done: bool = True, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The project with its tasks, as the list, Kanban and Gantt views need them."""
    sched.replan_if_dirty(db, user, debounce=False)
    p = own_project(db, user, pid)
    q = select(Task).where(Task.user_id == user.id, Task.project_id == p.id, Task.deleted_at.is_(None))
    if not include_done:
        q = q.where(Task.status.notin_(svc.DONE_STATES))
    tasks = db.scalars(q.order_by(Task.board_position, Task.created_at)).all()
    outs = svc.bulk_out(db, list(tasks))
    tracked = tracking.tracked_by_task(db, user, [t.id for t in tasks])
    items = []
    for o in outs:
        d = o.model_dump()
        d["tracked_min"] = tracked.get(o.id, 0)
        items.append(d)
    open_ = [o for o in outs if o.status not in svc.DONE_STATES]
    finishes = [o.scheduled_end for o in open_ if o.scheduled_end]
    po = ProjectOut.model_validate(p)
    po.open_count = len(open_)
    po.done_count = sum(1 for o in outs if o.status == "done")
    po.open_min = sum(o.remaining_min for o in open_)
    po.tracked_min = sum(tracked.values())
    return {
        "project": po.model_dump(),
        "tasks": items,
        "forecast_finish": max(finishes) if finishes else None,
        "unscheduled": sum(1 for o in open_ if o.auto_schedule and not o.scheduled_start),
        "at_risk": sum(1 for o in open_ if o.at_risk),
    }


class ReorderIn(BaseModel):
    task_ids: list[uuid.UUID] = Field(max_length=500)


@router.post("/tasks/reorder", status_code=204)
def reorder(body: ReorderIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Kanban card order. Only positions change, so this doesn't add history entries or trigger a replan."""
    for i, tid in enumerate(body.task_ids):
        db.execute(update(Task).where(Task.id == tid, Task.user_id == user.id).values(board_position=i).execution_options(synchronize_session=False))
    db.commit()


# -- bulk task creation (templates, AI breakdowns) ------------------------------------------------------------------


class OutlineItem(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    duration_min: int = Field(default=30, ge=5, le=60 * 40)
    priority: Priority | None = None
    offset_days: int | None = Field(default=None, ge=0, le=3650)
    depends_on: list[int] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    is_milestone: bool = False
    notes: str | None = None


class BulkIn(BaseModel):
    items: list[OutlineItem] = Field(min_length=1, max_length=60)
    start_date: date | None = None  # offsets count from here; defaults to the project's start or today
    from_ai: bool = False


def _check_outline(items: list[OutlineItem]) -> None:
    for i, it in enumerate(items):
        for d in it.depends_on:
            if d < 0 or d >= len(items) or d == i:
                raise HTTPException(422, f"Item {i + 1} depends on an item that isn't in the list")
            if d > i:
                raise HTTPException(422, f"Item {i + 1} depends on a later item; list prerequisites first")


def create_outline(db: Session, user: User, project: Project, items: list[OutlineItem], start: date, source: str) -> list[Task]:
    _check_outline(items)
    tz = ZoneInfo(user.timezone)
    created: list[Task] = []
    for i, it in enumerate(items):
        deadline = datetime.combine(start + timedelta(days=it.offset_days), time(17, 0), tz) if it.offset_days is not None else None
        data = TaskCreate(
            title=it.title, duration_min=it.duration_min, priority=it.priority or project.priority, project_id=project.id,
            category_id=project.category_id, deadline=deadline, checklist=it.checklist, is_milestone=it.is_milestone, notes=it.notes,
            board_position=i,
        )
        if it.is_milestone:
            data.auto_schedule = False
        created.append(svc.create_task(db, user, data, source=source))
    for i, it in enumerate(items):
        for d in it.depends_on:
            svc.add_dependency(db, user, created[i], created[d].id)
    return created


@router.post("/projects/{pid}/tasks/bulk", status_code=201)
def bulk_create(pid: uuid.UUID, body: BulkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = own_project(db, user, pid)
    start = body.start_date or p.start_date or datetime.now(ZoneInfo(user.timezone)).date()
    created = create_outline(db, user, p, body.items, start, "ai" if body.from_ai else "app")
    return [o.model_dump() for o in svc.bulk_out(db, created)]


# -- AI breakdown -----------------------------------------------------------------------------------------------------

BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "duration_min": {"type": "integer"},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["title", "duration_min"],
            },
        }
    },
    "required": ["tasks"],
}

BREAKDOWN_SYSTEM = (
    "You break a project into concrete tasks for a personal task manager that schedules them into a calendar. "
    "Return between 3 and {n} tasks in a sensible order. Each task has a short title that starts with a verb, an honest "
    "estimate in minutes (15 to 480; split anything bigger), and depends_on: the 0-based indexes of earlier tasks that "
    "must be finished first (often none). Don't include dates, people's names you weren't given, or tasks already listed."
)


class BreakdownIn(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)
    max_tasks: int = Field(default=10, ge=3, le=20)


@router.post("/projects/{pid}/breakdown")
def breakdown(pid: uuid.UUID, body: BreakdownIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Ask the AI for a task outline. Nothing is saved: the user reviews and edits it, then adds it with /tasks/bulk."""
    p = own_project(db, user, pid)
    provider = get_provider()
    cats = {c.id: c for c in db.scalars(select(Category).where(Category.user_id == user.id))}
    existing = db.scalars(select(Task).where(Task.project_id == p.id, Task.deleted_at.is_(None))).all()
    shown = [t.title for t in existing if privacy.visible(t, user, cats.get(t.category_id), p)][:40]
    lines = []
    if privacy.project_visible(p):
        lines.append(f"Project: {p.name}")
    lines.append(f"What it needs to achieve: {body.goal.strip()}")
    if shown:
        lines.append("Tasks already in the project (don't repeat):\n" + "\n".join(f"- {t}" for t in shown))
    try:
        raw = provider.structured(BREAKDOWN_SYSTEM.format(n=body.max_tasks), "\n".join(lines), BREAKDOWN_SCHEMA)
    except ProviderError as e:
        raise HTTPException(503, str(e)) from None
    items = []
    for i, t in enumerate((raw.get("tasks") or [])[: body.max_tasks]):
        title = str(t.get("title") or "").strip()[:500]
        if not title:
            continue
        try:
            dur = int(t.get("duration_min") or 30)
        except (TypeError, ValueError):
            dur = 30
        dur = max(15, min(480, int(round(dur / 15.0)) * 15 or 15))
        deps = sorted({int(d) for d in (t.get("depends_on") or []) if isinstance(d, (int, float)) and 0 <= int(d) < i})
        items.append({"title": title, "duration_min": dur, "depends_on": deps})
    if not items:
        raise HTTPException(502, "The AI didn't suggest any tasks. Try describing the goal in more detail.")
    return {"items": items, "provider": provider.name, "model": provider.model, "local": provider.local}


# -- templates ------------------------------------------------------------------------------------------------------

STARTERS = [
    {
        "key": "speaking",
        "name": "Speaking engagement",
        "description": "From accepting an invitation to the follow-up after the talk. Offsets count back from the event day you pick as the start.",
        "items": [
            {"title": "Confirm topic, length and audience with the organiser", "duration_min": 30, "offset_days": 0},
            {"title": "Outline the talk", "duration_min": 90, "offset_days": 7, "depends_on": [0]},
            {"title": "Draft slides", "duration_min": 240, "offset_days": 18, "depends_on": [1]},
            {"title": "Send bio, headshot and AV needs", "duration_min": 20, "offset_days": 10, "depends_on": [0]},
            {"title": "Rehearse twice, timed", "duration_min": 120, "offset_days": 25, "depends_on": [2]},
            {"title": "Book travel", "duration_min": 45, "offset_days": 14, "depends_on": [0]},
            {"title": "Give the talk", "duration_min": 60, "offset_days": 28, "depends_on": [4], "is_milestone": True},
            {"title": "Send thank-you note and slides", "duration_min": 20, "offset_days": 30, "depends_on": [6]},
        ],
    },
    {
        "key": "mediation",
        "name": "Mediation case",
        "description": "Intake to closing for one matter.",
        "items": [
            {"title": "Run conflict check", "duration_min": 20, "offset_days": 0},
            {"title": "Send engagement letter and agreement to mediate", "duration_min": 30, "offset_days": 2, "depends_on": [0]},
            {"title": "Review submissions from both sides", "duration_min": 180, "offset_days": 12, "depends_on": [1]},
            {"title": "Pre-mediation calls with each party", "duration_min": 120, "offset_days": 14, "depends_on": [2]},
            {"title": "Mediation session", "duration_min": 60, "offset_days": 21, "depends_on": [3], "is_milestone": True},
            {"title": "Draft settlement terms or closing note", "duration_min": 90, "offset_days": 23, "depends_on": [4]},
            {"title": "Send invoice and close file", "duration_min": 30, "offset_days": 30, "depends_on": [5]},
        ],
    },
    {
        "key": "paper",
        "name": "Academic paper",
        "description": "From literature review to submission.",
        "items": [
            {"title": "Literature review", "duration_min": 480, "offset_days": 14},
            {"title": "Write outline and thesis statement", "duration_min": 120, "offset_days": 18, "depends_on": [0]},
            {"title": "Draft sections", "duration_min": 960, "offset_days": 45, "depends_on": [1]},
            {"title": "Revise after a full read-through", "duration_min": 360, "offset_days": 55, "depends_on": [2]},
            {"title": "Check citations and formatting", "duration_min": 180, "offset_days": 60, "depends_on": [3]},
            {"title": "Submit", "duration_min": 30, "offset_days": 62, "depends_on": [4], "is_milestone": True},
        ],
    },
]


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category_id: uuid.UUID | None = None
    items: list[OutlineItem] = Field(default_factory=list, max_length=60)


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category_id: uuid.UUID | None = None
    items: list[OutlineItem] | None = Field(default=None, max_length=60)


class InstantiateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    start_date: date
    area_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None


class SaveAsTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


def _tpl_out(t: ProjectTemplate) -> dict:
    return {"id": str(t.id), "name": t.name, "description": t.description, "category_id": str(t.category_id) if t.category_id else None,
            "items": t.items, "updated_at": t.updated_at}


def _own_tpl(db: Session, user: User, tid: uuid.UUID) -> ProjectTemplate:
    t = db.get(ProjectTemplate, tid)
    if not t or t.user_id != user.id:
        raise HTTPException(404, "Template not found")
    return t


def _check_cat(db: Session, user: User, cid):
    if cid is not None:
        c = db.get(Category, cid)
        if not c or c.user_id != user.id:
            raise HTTPException(422, "Category not found")


def _items_json(items: list[OutlineItem]) -> list[dict]:
    _check_outline(items)
    return [i.model_dump(exclude_none=True) for i in items]


@router.get("/templates")
def list_templates(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ProjectTemplate).where(ProjectTemplate.user_id == user.id).order_by(ProjectTemplate.name)).all()
    return {"templates": [_tpl_out(t) for t in rows], "starters": STARTERS}


def _unique_name(db: Session, user: User, name: str, skip: uuid.UUID | None = None) -> None:
    q = select(ProjectTemplate).where(ProjectTemplate.user_id == user.id, ProjectTemplate.name == name)
    other = db.scalar(q)
    if other and other.id != skip:
        raise HTTPException(409, "A template with that name exists")


@router.post("/templates", status_code=201)
def create_template(body: TemplateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_cat(db, user, body.category_id)
    _unique_name(db, user, body.name)
    t = ProjectTemplate(user_id=user.id, name=body.name, description=body.description, category_id=body.category_id, items=_items_json(body.items))
    db.add(t)
    db.commit()
    return _tpl_out(t)


@router.post("/templates/starters/{key}", status_code=201)
def add_starter(key: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = next((x for x in STARTERS if x["key"] == key), None)
    if not s:
        raise HTTPException(404, "Starter template not found")
    _unique_name(db, user, s["name"])
    items = [OutlineItem.model_validate(i) for i in s["items"]]
    t = ProjectTemplate(user_id=user.id, name=s["name"], description=s["description"], items=_items_json(items))
    db.add(t)
    db.commit()
    return _tpl_out(t)


@router.patch("/templates/{tid}")
def patch_template(tid: uuid.UUID, body: TemplatePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = _own_tpl(db, user, tid)
    f = body.model_dump(exclude_unset=True)
    if "category_id" in f:
        _check_cat(db, user, f["category_id"])
    if f.get("name"):
        _unique_name(db, user, f["name"], skip=t.id)
    if "items" in f and body.items is not None:
        f["items"] = _items_json(body.items)
    for k, v in f.items():
        setattr(t, k, v)
    db.commit()
    return _tpl_out(t)


@router.delete("/templates/{tid}", status_code=204)
def delete_template(tid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_own_tpl(db, user, tid))
    db.commit()


@router.post("/templates/{tid}/instantiate", status_code=201)
def instantiate(tid: uuid.UUID, body: InstantiateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Create a project from a template: one task per item, deadlines counted from the start date, dependencies kept."""
    t = _own_tpl(db, user, tid)
    cat_id = body.category_id or t.category_id
    _check_cat(db, user, cat_id)
    if body.area_id:
        _own_area(db, user, body.area_id)
    items = [OutlineItem.model_validate(i) for i in t.items or []]
    offsets = [i.offset_days for i in items if i.offset_days is not None]
    tz = ZoneInfo(user.timezone)
    deadline = datetime.combine(body.start_date + timedelta(days=max(offsets)), time(17, 0), tz) if offsets else None
    p = Project(user_id=user.id, name=body.name, category_id=cat_id, area_id=body.area_id, start_date=body.start_date,
                template_id=t.id, deadline=deadline)
    db.add(p)
    db.commit()
    created = create_outline(db, user, p, items, body.start_date, "app")
    return {"project": ProjectOut.model_validate(p).model_dump(), "tasks_created": len(created)}


@router.post("/projects/{pid}/save-as-template", status_code=201)
def save_as_template(pid: uuid.UUID, body: SaveAsTemplateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Turn a project's tasks into a template. Deadlines become offsets from the project's start date."""
    p = own_project(db, user, pid)
    _unique_name(db, user, body.name)
    tasks = db.scalars(select(Task).where(Task.project_id == p.id, Task.deleted_at.is_(None), Task.status != "cancelled")
                       .order_by(Task.board_position, Task.created_at)).all()
    if not tasks:
        raise HTTPException(422, "The project has no tasks to save")
    tz = ZoneInfo(user.timezone)
    start = p.start_date or min(t.created_at for t in tasks).astimezone(tz).date()
    index = {t.id: i for i, t in enumerate(tasks)}
    deps: dict[uuid.UUID, list[uuid.UUID]] = {}
    for d in db.scalars(select(TaskDependency).where(TaskDependency.task_id.in_(list(index)))):
        deps.setdefault(d.task_id, []).append(d.depends_on_task_id)
    # Prerequisites must come first; order tasks so they do (stable for ties).
    order: list[Task] = []
    placed: set[uuid.UUID] = set()
    remaining = list(tasks)
    while remaining:
        progressed = False
        for t in list(remaining):
            if all(d in placed or d not in index for d in deps.get(t.id, [])):
                order.append(t)
                placed.add(t.id)
                remaining.remove(t)
                progressed = True
        if not progressed:  # cannot happen (cycles are rejected), but never loop forever
            order += remaining
            break
    pos = {t.id: i for i, t in enumerate(order)}
    items = []
    for t in order:
        off = max(0, (t.deadline.astimezone(tz).date() - start).days) if t.deadline else None
        items.append(OutlineItem(
            title=t.title, duration_min=max(5, t.duration_min), priority=t.priority, offset_days=off,
            depends_on=sorted(pos[d] for d in deps.get(t.id, []) if d in pos and pos[d] < pos[t.id]),
            checklist=[c.text for c in t.checklist], is_milestone=t.is_milestone,
        ))
    tpl = ProjectTemplate(user_id=user.id, name=body.name, description=body.description, category_id=p.category_id, items=_items_json(items))
    db.add(tpl)
    db.commit()
    return _tpl_out(tpl)
