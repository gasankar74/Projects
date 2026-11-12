"""Background worker: one process, no queue server needed.

- Every 2 seconds: replan users whose data changed at least 5 seconds ago (debounce; unchanged input is skipped),
  sync calendars Google told us changed (push notifications), and send pending task-block writes to Google
  inside a per-minute budget.
- Every 10 minutes: sync every connected Google account (the fallback when push isn't set up or a notification
  was missed), top up repeating tasks, and open or renew push channels.
- Every minute: notifications (deadline risks, sync failures, upcoming blocks) through the app, browser push and
  email, and the daily digest.
- Every hour: replan everyone (time moves on), reconcile task blocks in Google, generate repeating tasks, drop
  expired sessions and old read notifications.

It records a heartbeat in the database (shown by /healthz) and touches HEARTBEAT_FILE for Docker's health check.
"""

import logging
import signal
import time
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from .config import get_settings
from .db import session_factory
from .logging_setup import setup_logging
from .models import AppState, AuthSession, BlockEventLink, Calendar, GoogleAccount, ScheduleRun, User
from .services import calendar_write, notify, push, recurrence, scheduling
from .services.calendar_sync import sync_account
from .services.google import GoogleClient

log = logging.getLogger("meridian.worker")
SYNC_EVERY = timedelta(minutes=10)
REFRESH_EVERY = timedelta(hours=1)
PUSH_DEBOUNCE = timedelta(seconds=3)
NOTIFY_EVERY = timedelta(minutes=1)
HEARTBEAT_EVERY = timedelta(seconds=30)
HEARTBEAT_FILE = Path("/tmp/meridian-worker-alive")
NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _heartbeat(db, now: datetime) -> None:
    row = db.get(AppState, "worker")
    if row:
        row.value = {"at": now.isoformat()}
        row.updated_at = now
    else:
        db.add(AppState(key="worker", value={"at": now.isoformat()}, updated_at=now))
    db.commit()


def _write_budget(state: dict, now: datetime) -> int:
    sent: deque = state.setdefault("writes", deque())
    while sent and now - sent[0] > timedelta(minutes=1):
        sent.popleft()
    return max(0, get_settings().google_writes_per_min - len(sent))


def tick(db, client: GoogleClient | None, state: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)

    if now - state.get("heartbeat", NEVER) >= HEARTBEAT_EVERY:
        state["heartbeat"] = now
        try:
            _heartbeat(db, now)
        except Exception:
            db.rollback()
            log.exception("heartbeat failed")

    # Repeating tasks first, so a replan in this tick already sees new occurrences.
    if now - state.get("generated", NEVER) >= SYNC_EVERY:
        state["generated"] = now
        try:
            recurrence.generate_all(db, now)
        except Exception:
            db.rollback()
            log.exception("repeating-task generation failed")

    cutoff = now - timedelta(seconds=scheduling.DEBOUNCE_S)
    for user in db.scalars(select(User).where(User.schedule_dirty_at.is_not(None), User.schedule_dirty_at <= cutoff)).all():
        try:
            run, plan = scheduling.replan(db, user, trigger=user.schedule_dirty_reason or "change")
            log.info("replanned %s (%s): %s", user.email, run.trigger, "skipped, no change" if plan is None else f"{len(run.blocks)} blocks")
        except Exception:
            db.rollback()
            log.exception("replan failed for %s", user.id)

    if client:
        # Calendars Google says changed.
        flagged = db.scalars(select(Calendar).where(Calendar.sync_requested_at.is_not(None), Calendar.sync_requested_at <= now - PUSH_DEBOUNCE)).all()
        for acct_id in {c.google_account_id for c in flagged}:
            acct = db.get(GoogleAccount, acct_id)
            if acct and acct.status != "needs_reconnect":
                try:
                    sync_account(db, acct, client)
                except Exception:
                    db.rollback()
                    log.exception("push-triggered sync failed for %s", acct_id)

        # Task-block writes.
        budget = _write_budget(state, now)
        if budget:
            user_ids = set(db.scalars(select(BlockEventLink.user_id).where(BlockEventLink.state.in_(("pending", "delete")))).all())
            for uid in user_ids:
                user = db.get(User, uid)
                if not user or budget <= 0:
                    continue
                try:
                    n = calendar_write.flush(db, user, client, budget=budget, now=now)
                    budget -= n
                    state["writes"].extend([now] * n)
                except Exception:
                    db.rollback()
                    log.exception("writing blocks failed for %s", uid)

    if client and now - state.get("synced", NEVER) >= SYNC_EVERY:
        state["synced"] = now
        for acct in db.scalars(select(GoogleAccount).where(GoogleAccount.status != "needs_reconnect")).all():
            try:
                sync_account(db, acct, client)
            except Exception:
                db.rollback()
                log.exception("sync failed for account %s", acct.id)
        try:
            push.ensure_channels(db, client, now)
        except Exception:
            db.rollback()
            log.exception("push channel upkeep failed")

    if now - state.get("notified", NEVER) >= NOTIFY_EVERY:
        state["notified"] = now
        notify.tick(db, now)

    if now - state.get("refreshed", NEVER) >= REFRESH_EVERY:
        state["refreshed"] = now
        try:
            db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
            db.commit()
            notify.purge(db, now)
        except Exception:
            db.rollback()
            log.exception("clean-up failed")
        for user in db.scalars(select(User)).all():
            last = db.scalars(select(ScheduleRun.created_at).where(ScheduleRun.user_id == user.id).order_by(ScheduleRun.created_at.desc()).limit(1)).first()
            if last is None or now - last >= REFRESH_EVERY:
                scheduling.mark_dirty(user, "hourly refresh")
            if client:
                try:
                    calendar_write.reconcile(db, user, client, now)
                except Exception:
                    db.rollback()
                    log.exception("reconcile failed for %s", user.id)
        db.commit()


def run_forever() -> None:
    setup_logging()
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    client = GoogleClient()
    state: dict = {}
    log.info("worker started")
    while not stop["now"]:
        db = session_factory()()
        try:
            tick(db, client, state)
        except Exception:
            log.exception("worker tick failed")
        finally:
            db.close()
        try:
            HEARTBEAT_FILE.touch()
        except OSError:
            pass
        time.sleep(2)
    log.info("worker stopped")
