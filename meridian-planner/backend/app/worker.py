"""Background worker: one process, no queue server needed.

- Every 2 seconds: replan users whose data changed at least 5 seconds ago (debounce; unchanged input is skipped).
- Every 10 minutes: sync every connected Google account (a calendar change marks the user for a replan).
- Every hour: replan everyone, so finished-but-unmarked work and the passing of time roll forward.
"""

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .db import session_factory
from .models import GoogleAccount, ScheduleRun, User
from .services import scheduling
from .services.calendar_sync import sync_account
from .services.google import GoogleClient

log = logging.getLogger("meridian.worker")
SYNC_EVERY = timedelta(minutes=10)
REFRESH_EVERY = timedelta(hours=1)


def tick(db, client: GoogleClient | None, state: dict) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=scheduling.DEBOUNCE_S)
    for user in db.scalars(select(User).where(User.schedule_dirty_at.is_not(None), User.schedule_dirty_at <= cutoff)).all():
        try:
            run, plan = scheduling.replan(db, user, trigger=user.schedule_dirty_reason or "change")
            log.info("replanned %s (%s): %s", user.email, run.trigger, "skipped, no change" if plan is None else f"{len(run.blocks)} blocks")
        except Exception:
            db.rollback()
            log.exception("replan failed for %s", user.id)

    if client and now - state.get("synced", datetime.min.replace(tzinfo=timezone.utc)) >= SYNC_EVERY:
        state["synced"] = now
        for acct in db.scalars(select(GoogleAccount).where(GoogleAccount.status != "needs_reconnect")).all():
            try:
                sync_account(db, acct, client)
            except Exception:
                db.rollback()
                log.exception("sync failed for account %s", acct.id)

    if now - state.get("refreshed", datetime.min.replace(tzinfo=timezone.utc)) >= REFRESH_EVERY:
        state["refreshed"] = now
        for user in db.scalars(select(User)).all():
            last = db.scalars(select(ScheduleRun.created_at).where(ScheduleRun.user_id == user.id).order_by(ScheduleRun.created_at.desc()).limit(1)).first()
            if last is None or now - last >= REFRESH_EVERY:
                scheduling.mark_dirty(user, "hourly refresh")
        db.commit()


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
        time.sleep(2)
    log.info("worker stopped")
