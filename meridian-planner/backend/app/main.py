import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import assistant, auth, calendars, data, insights, organize, projects, schedule, series, tasks, tracking, webhooks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Meridian Planner API", version="0.4.0 (phase 4)")

# The web app proxies /api to this service, so cookies are first-party; CORS is only for local tools.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, tasks.router, organize.router, calendars.router, data.router, schedule.router, series.router, webhooks.router,
          assistant.router, projects.router, tracking.router, insights.router):
    app.include_router(r)


@app.get("/healthz")
def healthz():
    return {"ok": True}
