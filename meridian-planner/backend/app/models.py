"""Database models for Phase 1.

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


TASK_STATUSES = ("inbox", "todo", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("urgent", "high", "medium", "low")


class Task(TimestampMixin, Base):
    """The master task table. Moving a task between categories or projects only changes a foreign key."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_user_status", "user_id", "status"),)
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

    # Draft recurrence captured by quick add; the recurrence engine (Phase 3) turns it into a series.
    recurrence: Mapped[dict | None] = mapped_column(JSONB)

    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    links: Mapped[list] = mapped_column(JSONB, default=list)  # [{"title":..., "url":...}]
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_allowed: Mapped[bool] = mapped_column(Boolean, default=False)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default="app")  # app | quick_add | import | ai

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
