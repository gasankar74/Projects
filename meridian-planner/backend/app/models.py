"""Database models.

All instants are stored as timestamptz in UTC. Time-of-day rules keep an IANA zone.
Later phases add scheduling tables (schedule_runs, scheduled_blocks, ...) through migrations.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Privileged task details are never sent to an AI provider unless this is true (or the task opts in).
    ai_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when something the scheduler depends on changed; the worker replans a few seconds later (debounce).
    schedule_dirty_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schedule_dirty_reason: Mapped[str | None] = mapped_column(String(200))


class AuthSession(TimestampMixin, Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str] = mapped_column(String(400), default="")


class GoogleAccount(TimestampMixin, Base):
    """One connected Google login. A user may connect several (personal, business, family)."""

    __tablename__ = "google_accounts"
    __table_args__ = (UniqueConstraint("user_id", "google_sub"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    google_sub: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(320))
    scopes: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ok | syncing | error | needs_reconnect
    status: Mapped[str] = mapped_column(String(32), default="ok")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    calendars: Mapped[list["Calendar"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class Calendar(TimestampMixin, Base):
    __tablename__ = "calendars"
    __table_args__ = (UniqueConstraint("google_account_id", "google_calendar_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    google_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("google_accounts.id", ondelete="CASCADE"))
    google_calendar_id: Mapped[str] = mapped_column(String(320))
    summary: Mapped[str] = mapped_column(String(400), default="")
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(String(16))
    # owner | writer | reader | freeBusyReader  (from Google; reader and freeBusyReader are never written)
    access_role: Mapped[str] = mapped_column(String(32))
    time_zone: Mapped[str | None] = mapped_column(String(64))
    primary: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether this calendar's events are read and shown.
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    # Whether its busy events block scheduling time.
    blocks_time: Mapped[bool] = mapped_column(Boolean, default=True)
    # Where scheduled task blocks are written (Phase 3). Only allowed on writable calendars.
    is_task_target: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_token: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Set by a push notification from Google; the worker syncs the calendar a few seconds later.
    sync_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[GoogleAccount] = relationship(back_populates="calendars")

    @property
    def writable(self) -> bool:
        return self.access_role in ("owner", "writer")


class CalendarEvent(TimestampMixin, Base):
    """Local mirror of a Google event. Unique on (calendar_id, google_event_id): the main duplicate guard."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "google_event_id"),
        Index("ix_calendar_events_user_range", "user_id", "start_at", "end_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    calendar_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"), index=True)
    google_event_id: Mapped[str] = mapped_column(String(1024))
    ical_uid: Mapped[str | None] = mapped_column(String(1024))
    recurring_event_id: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), default="confirmed")  # confirmed | tentative | cancelled
    summary: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(Text)
    html_link: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)  # exclusive, as Google sends it
    time_zone: Mapped[str | None] = mapped_column(String(64))
    transparency: Mapped[str] = mapped_column(String(16), default="opaque")  # opaque = busy, transparent = free
    visibility: Mapped[str] = mapped_column(String(16), default="default")
    self_response: Mapped[str | None] = mapped_column(String(32))  # accepted | declined | tentative | needsAction
    etag: Mapped[str | None] = mapped_column(String(200))
    # True for blocks this app wrote (Phase 3); such events never count as busy time for the scheduler.
    is_task_block: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def is_busy(self) -> bool:
        if self.status == "cancelled" or self.transparency == "transparent" or self.is_task_block:
            return False
        if self.self_response == "declined":
            return False
        return True


class SchedulingProfile(TimestampMixin, Base):
    """Named working hours (e.g. Legal 9-5 weekdays, Personal evenings and weekends) with an optional daily cap.
    A task uses its own profile, else its category's, else the default work hours."""

    __tablename__ = "scheduling_profiles"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    color: Mapped[str | None] = mapped_column(String(16))
    work_hours: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"mon": [["09:00","12:00"],["13:00","17:00"]], ...}
    daily_cap_min: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)


class RecurringSeries(TimestampMixin, Base):
    """A repeating task. Each occurrence is an ordinary task row (series_id + occurrence_due), generated ahead of
    time for fixed rules, or when the previous one is completed for completion-based rules."""

    __tablename__ = "recurring_series"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    template: Mapped[dict] = mapped_column(JSONB, default=dict)  # task fields copied to every occurrence
    rule: Mapped[dict] = mapped_column(JSONB, default=dict)  # see app/scheduler/recurrence.Rule
    anchor: Mapped[date] = mapped_column(Date)  # first possible occurrence date
    missed_policy: Mapped[str] = mapped_column(String(16), default="keep")  # keep | skip | latest_only
    generate_ahead_days: Mapped[int] = mapped_column(Integer, default=14)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_generated_due: Mapped[date | None] = mapped_column(Date)
    generated_count: Mapped[int] = mapped_column(Integer, default=0)


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))


class Area(TimestampMixin, Base):
    __tablename__ = "areas"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Category(TimestampMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    color: Mapped[str] = mapped_column(String(16), default="#6366f1")
    position: Mapped[int] = mapped_column(Integer, default=0)
    default_priority: Mapped[str] = mapped_column(String(16), default="medium")
    # Phase 3 links a scheduling profile here.
    scheduling_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # What the AI assistant may read of this category's tasks: default (confidential tasks hidden) | hidden (every
    # task hidden) | allowed (confidential tasks visible too).
    ai_access: Mapped[str] = mapped_column(String(16), default="default", server_default="default")


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    area_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("areas.id", ondelete="SET NULL"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | on_hold | done | archived
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    color: Mapped[str | None] = mapped_column(String(16))
    client: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_templates.id", ondelete="SET NULL", name="fk_projects_template"))
    ai_access: Mapped[str] = mapped_column(String(16), default="default", server_default="default")


TASK_STATUSES = ("inbox", "todo", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("urgent", "high", "medium", "low")


class Task(TimestampMixin, Base):
    """The master task table. Moving a task between categories or projects only changes a foreign key."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_user_status", "user_id", "status"),
        Index("ix_tasks_user_external_ref", "user_id", "external_ref", postgresql_where=text("external_ref IS NOT NULL")),
        # One task per series per due date: generation is idempotent even if two workers race.
        UniqueConstraint("series_id", "occurrence_due", name="uq_tasks_series_occurrence"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="inbox")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    client: Mapped[str | None] = mapped_column(String(200))

    # Deadlines. A hard deadline is a constraint; a soft one is a target the scheduler may miss with a warning.
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_type: Mapped[str] = mapped_column(String(8), default="hard")  # hard | soft
    preferred_date: Mapped[date | None] = mapped_column(Date)
    earliest_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    remaining_min: Mapped[int] = mapped_column(Integer, default=30)
    completion_pct: Mapped[int] = mapped_column(Integer, default=0)

    # Scheduling rules (used by the engine from Phase 2).
    can_split: Mapped[bool] = mapped_column(Boolean, default=False)
    min_session_min: Mapped[int | None] = mapped_column(Integer)
    max_session_min: Mapped[int | None] = mapped_column(Integer)
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False)  # fixed time vs flexible
    pinned_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_schedule: Mapped[bool] = mapped_column(Boolean, default=True)
    preferred_time_of_day: Mapped[str | None] = mapped_column(String(16))  # morning | afternoon | evening
    allowed_days: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))  # 0 = Monday
    windows: Mapped[list | None] = mapped_column(JSONB)  # [{"days":[0..6],"start":"09:00","end":"12:00"}]
    buffer_before_min: Mapped[int] = mapped_column(Integer, default=0)
    buffer_after_min: Mapped[int] = mapped_column(Integer, default=0)
    energy: Mapped[str | None] = mapped_column(String(16))  # high | medium | low
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calendars.id", ondelete="SET NULL"))
    scheduling_profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scheduling_profiles.id", ondelete="SET NULL"))
    location: Mapped[str | None] = mapped_column(Text)
    travel_min: Mapped[int] = mapped_column(Integer, default=0)  # each way

    # Repeat rule as entered. Saving a task with one creates (or updates) its RecurringSeries.
    recurrence: Mapped[dict | None] = mapped_column(JSONB)
    series_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recurring_series.id", ondelete="SET NULL"), index=True)
    occurrence_due: Mapped[date | None] = mapped_column(Date)

    # Phase 4: order within a Kanban column, and milestones (zero-length markers on the Gantt chart).
    board_position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    links: Mapped[list] = mapped_column(JSONB, default=list)  # [{"title":..., "url":...}]
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_allowed: Mapped[bool] = mapped_column(Boolean, default=False)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default="app")  # app | quick_add | import | ai
    # Where an imported task came from ("motion:<id>" or "csv:<row hash>"), so importing the same file twice
    # doesn't duplicate it.
    external_ref: Mapped[str | None] = mapped_column(String(200))

    checklist: Mapped[list["ChecklistItem"]] = relationship(
        cascade="all, delete-orphan", order_by="ChecklistItem.position"
    )


class ChecklistItem(TimestampMixin, Base):
    __tablename__ = "checklist_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(500))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "depends_on_task_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attachment(TimestampMixin, Base):
    __tablename__ = "attachments"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)


class TaskEvent(Base):
    """Append-only change history and audit log for tasks."""

    __tablename__ = "task_events"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(16), default="user")  # user | ai | system | scheduler
    action: Mapped[str] = mapped_column(String(32))
    diff: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Account-level audit trail (sign-ins, calendar connections, exports, deletions)."""

    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduleRun(Base):
    """One run of the scheduler. The newest applied run is the current plan; older runs allow undo."""

    __tablename__ = "schedule_runs"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    trigger: Mapped[str] = mapped_column(String(200), default="manual")
    input_hash: Mapped[str] = mapped_column(String(64))
    applied: Mapped[bool] = mapped_column(Boolean, default=True)
    restored_from_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    overload: Mapped[dict | None] = mapped_column(JSONB)
    task_stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    days: Mapped[list] = mapped_column(JSONB, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    blocks: Mapped[list["ScheduledBlock"]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="ScheduledBlock.start_at")


class ScheduledBlock(Base):
    __tablename__ = "scheduled_blocks"
    __table_args__ = (Index("ix_scheduled_blocks_user_run", "user_id", "run_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schedule_runs.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_index: Mapped[int] = mapped_column(Integer, default=1)
    session_count: Mapped[int] = mapped_column(Integer, default=1)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[dict] = mapped_column(JSONB, default=dict)

    run: Mapped[ScheduleRun] = relationship(back_populates="blocks")


class BlockEventLink(Base):
    """A scheduled block as written to Google (and the outbox for writing it).

    `desired_*` is what the plan wants; `written_*` is what Google last accepted. A row whose desired state differs
    from its written state, or that is marked for deletion, is pending work for the writer. The Google event id is
    derived from this row, so a retried insert gets 409 (already exists) instead of a duplicate event."""

    __tablename__ = "block_event_links"
    __table_args__ = (
        UniqueConstraint("calendar_id", "google_event_id"),
        Index("ix_block_links_pending", "state", "next_attempt_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    calendar_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    slot: Mapped[int] = mapped_column(Integer, default=1)  # session number within the task's planned blocks
    google_event_id: Mapped[str] = mapped_column(String(128))
    desired_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    desired_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    desired_title: Mapped[str] = mapped_column(String(500), default="")
    desired_private: Mapped[bool] = mapped_column(Boolean, default=False)
    written_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    written_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    written_title: Mapped[str | None] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(200))
    # pending (needs writing) | synced | delete (needs removing from Google) | error | archived (in the past; kept)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WatchChannel(Base):
    """A Google push-notification channel for one calendar. The token is stored hashed."""

    __tablename__ = "watch_channels"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    calendar_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), unique=True)
    resource_id: Mapped[str | None] = mapped_column(String(256))
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectTemplate(TimestampMixin, Base):
    """A reusable project outline. `items` is a list of task outlines:
    {"title", "duration_min", "priority", "offset_days" (due date, days after the project start), "depends_on": [index],
     "checklist": [str], "is_milestone"}."""

    __tablename__ = "project_templates"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    items: Mapped[list] = mapped_column(JSONB, default=list)


class TimeEntry(Base):
    """Tracked work on a task. A running timer has no ended_at; there is at most one per user."""

    __tablename__ = "time_entries"
    __table_args__ = (
        Index("ix_time_entries_user_start", "user_id", "started_at"),
        Index("uq_time_entries_running", "user_id", unique=True, postgresql_where=text("ended_at IS NULL")),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="timer")  # timer | manual | focus
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def minutes(self) -> int:
        end = self.ended_at or utcnow()
        return max(0, int((end - self.started_at).total_seconds() // 60))


class DailyFocus(Base):
    """The user's own Top 3 for a day, when they pick it instead of taking the suggested one."""

    __tablename__ = "daily_focus"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    task_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AiConversation(TimestampMixin, Base):
    __tablename__ = "ai_conversations"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    # Short task references shown to the model ("T1" -> task uuid), stable for the whole conversation.
    refs: Mapped[dict] = mapped_column(JSONB, default=dict)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(100), default="")

    messages: Mapped[list["AiMessage"]] = relationship(cascade="all, delete-orphan", order_by="AiMessage.seq")


class AiMessage(Base):
    """One transcript entry. role: user | assistant | tool. `changes` lists the records a tool call saved, read back
    from the database, so the app (not the model) states what was actually changed."""

    __tablename__ = "ai_messages"
    __table_args__ = (Index("ix_ai_messages_conv_seq", "conversation_id", "seq"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSONB)  # assistant: [{"id","name","arguments"}]
    tool_call_id: Mapped[str | None] = mapped_column(String(100))
    tool_name: Mapped[str | None] = mapped_column(String(64))
    changes: Mapped[list | None] = mapped_column(JSONB)
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AiAction(Base):
    """A change the assistant proposed that needs the user's confirmation (bulk, delete, or moving work to another
    day). Nothing is written until the user confirms."""

    __tablename__ = "ai_actions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict] = mapped_column(JSONB, default=dict)  # resolved: real task ids
    summary: Mapped[str] = mapped_column(Text, default="")
    preview: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | confirmed | cancelled | failed
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# -- Phase 5: notifications, imports, app state -------------------------------------------------------------------


class Notification(Base):
    """Something the user should know about. Always shown in the app; also sent by browser push or email
    depending on the user's settings. dedupe_key makes generation idempotent (one per deadline, per outage...)."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_dedupe"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(400))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Channel -> outcome, e.g. {"push": "sent", "email": "quiet_hours"}. Empty until delivery has been decided.
    channels: Mapped[dict] = mapped_column(JSONB, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushSubscription(Base):
    """A browser's Web Push subscription (one per browser/device)."""

    __tablename__ = "push_subscriptions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)


class ImportJob(TimestampMixin, Base):
    """A CSV import (Motion or generic) waiting for review, then applied or cancelled.
    `raw` holds the uploaded text only until the import is applied or cancelled."""

    __tablename__ = "imports"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="motion_csv")
    filename: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(16), default="review")  # review | applied | cancelled
    raw: Mapped[str | None] = mapped_column(Text)
    mapping: Mapped[dict] = mapped_column(JSONB, default=dict)
    options: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Per-row decisions the user made in the review screen: {"<row index>": "import" | "skip"}.
    decisions: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppState(Base):
    """Small key-value store for process state that must survive restarts (worker heartbeat, last digest...)."""

    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
