import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

Priority = Literal["urgent", "high", "medium", "low"]
Status = Literal["inbox", "todo", "in_progress", "blocked", "done", "cancelled"]
AiAccess = Literal["default", "hidden", "allowed"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    name: str
    timezone: str
    ai_opt_in: bool


class UserUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    ai_opt_in: bool | None = None

    @field_validator("timezone")
    @classmethod
    def valid_tz(cls, v):
        if v is None:
            return v
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"Unknown time zone {v!r}")
        return v


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    color: str = "#6366f1"
    position: int = 0
    default_priority: Priority = "medium"
    scheduling_profile_id: uuid.UUID | None = None
    ai_access: AiAccess = "default"


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = None
    position: int | None = None
    default_priority: Priority | None = None
    scheduling_profile_id: uuid.UUID | None = None
    ai_access: AiAccess | None = None


class CategoryOut(ORM):
    id: uuid.UUID
    name: str
    color: str
    position: int
    default_priority: str
    scheduling_profile_id: uuid.UUID | None = None
    ai_access: str = "default"
    open_count: int = 0


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category_id: uuid.UUID | None = None
    status: Literal["active", "on_hold", "done", "archived"] = "active"
    priority: Priority = "medium"
    deadline: datetime | None = None
    color: str | None = None
    client: str | None = None
    notes: str | None = None
    area_id: uuid.UUID | None = None
    start_date: date | None = None
    position: int = 0
    ai_access: AiAccess = "default"


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: uuid.UUID | None = None
    status: Literal["active", "on_hold", "done", "archived"] | None = None
    priority: Priority | None = None
    deadline: datetime | None = None
    color: str | None = None
    client: str | None = None
    notes: str | None = None
    area_id: uuid.UUID | None = None
    start_date: date | None = None
    position: int | None = None
    ai_access: AiAccess | None = None


class ProjectOut(ORM):
    id: uuid.UUID
    name: str
    category_id: uuid.UUID | None
    status: str
    priority: str
    deadline: datetime | None
    color: str | None
    client: str | None
    notes: str | None
    area_id: uuid.UUID | None = None
    start_date: date | None = None
    position: int = 0
    template_id: uuid.UUID | None = None
    ai_access: str = "default"
    open_count: int = 0
    done_count: int = 0
    open_min: int = 0
    tracked_min: int = 0


class Window(BaseModel):
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    start: str = "09:00"
    end: str = "17:00"


class Link(BaseModel):
    title: str = ""
    url: HttpUrl


class ChecklistItemOut(ORM):
    id: uuid.UUID
    text: str
    done: bool
    position: int


class TaskFields(BaseModel):
    """Every editable task field. All optional so the same model serves create and patch."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    notes: str | None = None
    status: Status | None = None
    priority: Priority | None = None
    category_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    client: str | None = None
    deadline: datetime | None = None
    deadline_type: Literal["hard", "soft"] | None = None
    preferred_date: date | None = None
    earliest_start: datetime | None = None
    duration_min: int | None = Field(default=None, ge=5, le=60 * 200)
    remaining_min: int | None = Field(default=None, ge=0, le=60 * 200)
    completion_pct: int | None = Field(default=None, ge=0, le=100)
    can_split: bool | None = None
    min_session_min: int | None = Field(default=None, ge=5)
    max_session_min: int | None = Field(default=None, ge=5)
    is_fixed: bool | None = None
    pinned_start: datetime | None = None
    locked: bool | None = None
    auto_schedule: bool | None = None
    preferred_time_of_day: Literal["morning", "afternoon", "evening"] | None = None
    allowed_days: list[int] | None = None
    windows: list[Window] | None = None
    buffer_before_min: int | None = Field(default=None, ge=0, le=480)
    buffer_after_min: int | None = Field(default=None, ge=0, le=480)
    energy: Literal["high", "medium", "low"] | None = None
    calendar_id: uuid.UUID | None = None
    scheduling_profile_id: uuid.UUID | None = None
    location: str | None = Field(default=None, max_length=500)
    travel_min: int | None = Field(default=None, ge=0, le=480)
    recurrence: dict | None = None
    tags: list[str] | None = None
    links: list[Link] | None = None
    confidential: bool | None = None
    ai_allowed: bool | None = None
    board_position: int | None = None
    is_milestone: bool | None = None

    @field_validator("tags")
    @classmethod
    def norm_tags(cls, v):
        if v is None:
            return v
        return list(dict.fromkeys(t.strip().lower().lstrip("#") for t in v if t.strip()))

    @field_validator("allowed_days")
    @classmethod
    def days_range(cls, v):
        if v is not None and any(d < 0 or d > 6 for d in v):
            raise ValueError("allowed_days uses 0 (Monday) to 6 (Sunday)")
        return v


class TaskCreate(TaskFields):
    title: str = Field(min_length=1, max_length=500)
    checklist: list[str] = Field(default_factory=list)


class TaskOut(ORM):
    id: uuid.UUID
    title: str
    description: str | None
    notes: str | None
    status: str
    priority: str
    category_id: uuid.UUID | None
    project_id: uuid.UUID | None
    parent_task_id: uuid.UUID | None
    client: str | None
    deadline: datetime | None
    deadline_type: str
    preferred_date: date | None
    earliest_start: datetime | None
    duration_min: int
    remaining_min: int
    completion_pct: int
    can_split: bool
    min_session_min: int | None
    max_session_min: int | None
    is_fixed: bool
    pinned_start: datetime | None
    locked: bool
    auto_schedule: bool
    preferred_time_of_day: str | None
    allowed_days: list[int] | None
    windows: list | None
    buffer_before_min: int
    buffer_after_min: int
    energy: str | None
    calendar_id: uuid.UUID | None
    scheduling_profile_id: uuid.UUID | None = None
    location: str | None = None
    travel_min: int = 0
    recurrence: dict | None
    series_id: uuid.UUID | None = None
    occurrence_due: date | None = None
    tags: list[str]
    links: list
    confidential: bool
    ai_allowed: bool
    board_position: int = 0
    is_milestone: bool = False
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source: str
    checklist: list[ChecklistItemOut] = []
    depends_on: list[uuid.UUID] = []
    blocking: list[uuid.UUID] = []
    overdue: bool = False
    # From the current plan (None when the task isn't scheduled).
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    schedule_status: str | None = None  # scheduled | partial | unscheduled | late
    at_risk: bool = False
    score: float | None = None


class TaskList(BaseModel):
    items: list[TaskOut]
    total: int


class TaskEventOut(ORM):
    id: uuid.UUID
    actor: str
    action: str
    diff: dict
    at: datetime


class QuickAddIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    preview: bool = False


class QuickAddOut(BaseModel):
    parsed: dict
    understood: list[str]
    task: TaskOut | None = None


class ChecklistIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ChecklistPatch(BaseModel):
    text: str | None = None
    done: bool | None = None
    position: int | None = None


class DependencyIn(BaseModel):
    depends_on_task_id: uuid.UUID


class GoogleAccountOut(ORM):
    id: uuid.UUID
    email: str
    status: str
    last_error: str | None
    last_synced_at: datetime | None


class CalendarOut(ORM):
    id: uuid.UUID
    google_account_id: uuid.UUID
    summary: str
    color: str | None
    access_role: str
    time_zone: str | None
    primary: bool
    selected: bool
    blocks_time: bool
    is_task_target: bool
    writable: bool
    last_synced_at: datetime | None
    last_error: str | None
    push_enabled: bool = False


class CalendarPatch(BaseModel):
    selected: bool | None = None
    blocks_time: bool | None = None
    is_task_target: bool | None = None


class EventOut(ORM):
    id: uuid.UUID
    calendar_id: uuid.UUID
    summary: str
    location: str | None
    html_link: str | None
    start_at: datetime | None
    end_at: datetime | None
    all_day: bool
    start_date: date | None
    end_date: date | None
    transparency: str
    visibility: str
    status: str
    is_busy: bool
    color: str | None = None
    calendar_name: str | None = None
    read_only: bool = True


class BusyOut(BaseModel):
    start: datetime
    end: datetime


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    color: str | None = None
    work_hours: dict[str, list[list[str]]] = Field(default_factory=dict)
    daily_cap_min: int | None = Field(default=None, ge=15, le=24 * 60)
    position: int = 0


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = None
    work_hours: dict[str, list[list[str]]] | None = None
    daily_cap_min: int | None = Field(default=None, ge=15, le=24 * 60)
    position: int | None = None


class ProfileOut(ORM):
    id: uuid.UUID
    name: str
    color: str | None
    work_hours: dict
    daily_cap_min: int | None
    position: int


class SeriesPatch(BaseModel):
    """Changes to a repeating task. Template fields apply to future occurrences that haven't been started."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    rule: dict | None = None
    missed_policy: Literal["keep", "skip", "latest_only"] | None = None
    generate_ahead_days: int | None = Field(default=None, ge=1, le=120)
    active: bool | None = None
    template: dict | None = None


class SeriesOut(ORM):
    id: uuid.UUID
    title: str
    template: dict
    rule: dict
    anchor: date
    missed_policy: str
    generate_ahead_days: int
    active: bool
    last_generated_due: date | None
    generated_count: int
    description: str = ""
    upcoming: list[date] = []
    open_count: int = 0


class RulePreviewIn(BaseModel):
    rule: dict
    start: date | None = None
    count: int = Field(default=6, ge=1, le=40)
