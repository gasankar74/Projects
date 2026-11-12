import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .config import get_settings
from .db import session_factory
from .hardening import HardeningMiddleware, production_problems
from .logging_setup import setup_logging
from .models import AppState
from .routers import (
    activity, assistant, auth, calendars, data, imports, insights, notifications, organize, projects, schedule, series,
    tasks, tracking, webhooks,
)

setup_logging()
log = logging.getLogger("meridian.api")
VERSION = "0.5.0 (phase 5)"


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = production_problems()
    if problems:
        for p in problems:
            log.error("unsafe production setting: %s", p)
        raise RuntimeError("Refusing to start in production with unsafe settings: " + "; ".join(problems))
    log.info("Meridian Planner API %s starting (%s)", VERSION, get_settings().env)
    yield


app = FastAPI(title="Meridian Planner API", version=VERSION, lifespan=lifespan)

# The web app proxies /api to this service, so cookies are first-party; CORS is only for local tools.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(HardeningMiddleware)

for r in (auth.router, tasks.router, organize.router, calendars.router, data.router, schedule.router, series.router, webhooks.router,
          assistant.router, projects.router, tracking.router, insights.router, imports.router, notifications.router, activity.router):
    app.include_router(r)


@app.get("/healthz")
def healthz():
    """For Docker and monitoring: the database answers, and when the worker last checked in."""
    out: dict = {"ok": True, "version": VERSION}
    try:
        db = session_factory()()
        try:
            db.execute(text("SELECT 1"))
            hb = db.get(AppState, "worker")
            if hb:
                age = (datetime.now(timezone.utc) - hb.updated_at).total_seconds()
                out["worker_seen_s"] = round(age)
                out["worker_ok"] = age < 120
        finally:
            db.close()
        out["db"] = "ok"
    except Exception as e:
        log.error("health check: database unavailable: %s", e)
        return JSONResponse({"ok": False, "db": "unavailable"}, status_code=503)
    return out
