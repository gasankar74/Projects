"""Non-functional requirement from the plan: a replan of 500 open tasks over a 6-week horizon finishes in under
5 seconds (database read, engine, optimiser and saving the plan included)."""

import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.db import session_factory
from app.models import Calendar, CalendarEvent, GoogleAccount, Task, TaskDependency, User
from app.services import scheduling

from .conftest import login


def test_replan_500_tasks_under_5_seconds(client):
    login(client)
    rnd = random.Random(42)
    now = datetime.now(timezone.utc)
    with session_factory()() as db:
        user = db.query(User).one()
        scheduling.update_settings(db, user, {"horizon_days": 42})
        acct = GoogleAccount(user_id=user.id, google_sub="perf", email="perf@example.com")
        db.add(acct)
        db.flush()
        cal = Calendar(user_id=user.id, google_account_id=acct.id, google_calendar_id="perf", summary="Work", access_role="owner")
        db.add(cal)
        db.flush()
        # About 4 meetings a working day for 6 weeks.
        for d in range(42):
            day = (now + timedelta(days=d)).replace(hour=13, minute=0, second=0, microsecond=0)
            if day.weekday() >= 5:
                continue
            for k in range(4):
                start = day + timedelta(hours=k * 2, minutes=rnd.choice([0, 30]))
                db.add(CalendarEvent(user_id=user.id, calendar_id=cal.id, google_event_id=f"e{d}-{k}", summary="Meeting",
                                     start_at=start, end_at=start + timedelta(minutes=rnd.choice([30, 60]))))
        ids = []
        for i in range(500):
            dur = rnd.choice([15, 30, 45, 60, 90, 120, 180])
            t = Task(
                id=uuid.uuid4(), user_id=user.id, title=f"Task {i}", status="todo", priority=rnd.choice(["urgent", "high", "medium", "medium", "low"]),
                duration_min=dur, remaining_min=dur, can_split=dur >= 90, min_session_min=30 if dur >= 90 else None,
                deadline=(now + timedelta(days=rnd.randint(1, 40), hours=rnd.randint(0, 8))) if rnd.random() < 0.6 else None,
                deadline_type=rnd.choice(["hard", "soft"]), tags=[], links=[],
            )
            db.add(t)
            ids.append(t.id)
        db.flush()
        for i in range(0, 60, 2):  # some dependency chains
            db.add(TaskDependency(task_id=ids[i + 1], depends_on_task_id=ids[i]))
        db.commit()

        started = time.perf_counter()
        run, _ = scheduling.replan(db, user, trigger="perf", force=True)
        elapsed = time.perf_counter() - started
        n_blocks = len(run.blocks)
        overloaded = run.overload is not None
        print(f"replan of 500 tasks: {elapsed:.2f}s, {n_blocks} blocks, engine {run.duration_ms} ms")
    # ~580 hours of work against ~190 free hours: heavily overloaded on purpose, so the overload advice (the
    # slowest part) runs too.
    assert n_blocks > 150 and overloaded
    assert elapsed < 5.0, f"replan took {elapsed:.2f}s"
