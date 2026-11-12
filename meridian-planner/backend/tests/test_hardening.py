"""Rate limits, the cross-site request check, body size cap, security headers, production start-up checks,
health check, audit views, sessions and account deletion clean-up."""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session_factory
from app.hardening import limiter, production_problems
from app.models import AuditLog, User

from .conftest import login
from .test_calendar_write import connect, ours, write
from .test_schedule_api import schedule, task


@pytest.fixture
def limits():
    s = get_settings()
    s.rate_limits_enabled = True
    limiter.reset()
    yield
    s.rate_limits_enabled = False
    limiter.reset()


def test_sign_in_is_rate_limited_per_address(client, limits):
    codes = [client.post("/auth/dev-login", json={"email": f"u{i}@example.com"}).status_code for i in range(22)]
    assert codes[:20] == [200] * 20 and codes[20] == 429
    r = client.post("/auth/dev-login", json={"email": "x@example.com"})
    assert int(r.headers["Retry-After"]) > 0 and "Too many requests" in r.json()["detail"]


def test_ai_calls_are_limited_per_user(authed, limits):
    conv = authed.post("/assistant/conversations", json={}).json()
    codes = [authed.post(f"/assistant/conversations/{conv['id']}/messages", json={"text": "hi"}).status_code for _ in range(17)]
    assert 429 in codes and codes.index(429) == 15
    # Other requests from the same user still work.
    assert authed.get("/tasks").status_code == 200


def test_cross_site_writes_are_blocked(authed):
    t = authed.post("/tasks", json={"title": "x"}, headers={"Origin": "https://evil.example.com"})
    assert t.status_code == 403
    assert authed.post("/tasks", json={"title": "x"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    # Same origin as the app (or as the proxy's forwarded host) is fine; reads are never blocked.
    assert authed.post("/tasks", json={"title": "x"}, headers={"Origin": get_settings().frontend_url}).status_code == 201
    assert authed.post("/tasks", json={"title": "y"}, headers={"Origin": "https://planner.tail1234.ts.net", "X-Forwarded-Host": "planner.tail1234.ts.net"}).status_code == 201
    assert authed.get("/tasks", headers={"Origin": "https://evil.example.com"}).status_code == 200


def test_body_size_cap_and_headers(authed):
    r = authed.post("/imports", content=b"x" * 10, headers={"Content-Length": str(get_settings().max_body_bytes + 1), "Content-Type": "application/json"})
    assert r.status_code == 413
    r = authed.get("/tasks")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"


def test_production_refuses_unsafe_settings():
    s = get_settings()
    saved = (s.env, s.dev_login_enabled, s.cookie_secure, s.frontend_url, s.allowed_emails)
    try:
        s.env = "production"
        problems = production_problems()
        assert any("DEV_LOGIN" in p for p in problems) and any("COOKIE_SECURE" in p for p in problems) and any("https" in p for p in problems)
        assert any("ALLOWED_EMAILS" in p for p in problems)
        s.dev_login_enabled, s.cookie_secure, s.frontend_url = False, True, "https://planner.example.net"
        s.allowed_emails = "you@example.com"
        assert production_problems() == []
    finally:
        s.env, s.dev_login_enabled, s.cookie_secure, s.frontend_url, s.allowed_emails = saved


def test_healthz_reports_db_and_worker(client):
    from datetime import datetime, timezone

    from app.worker import _heartbeat

    r = client.get("/healthz").json()
    assert r["ok"] is True and r["db"] == "ok" and "worker_seen_s" not in r
    with session_factory()() as db:
        _heartbeat(db, datetime.now(timezone.utc))
    r = client.get("/healthz").json()
    assert r["worker_ok"] is True and r["worker_seen_s"] < 5


def test_activity_log_merges_account_and_task_history(authed):
    t = task(authed, title="Board memo")
    authed.patch(f"/tasks/{t['id']}", json={"priority": "high"})
    authed.patch("/schedule/settings", json={"daily_cap_min": 300})
    authed.get("/export/tasks.csv")
    items = authed.get("/activity").json()["items"]
    labels = [i["label"] for i in items]
    assert "Exported tasks (CSV)" in labels and "Changed scheduling settings" in labels and "Edited" in labels and "Created" in labels
    edited = next(i for i in items if i["label"] == "Edited")
    assert edited["task_title"] == "Board memo" and "priority" in edited["detail"]["changed"]
    assert all(i["source"] == "task" for i in authed.get("/activity?kind=tasks").json()["items"])
    assert authed.get("/activity?kind=ai").json()["items"] == []
    # Paging
    page = authed.get("/activity?limit=2").json()
    assert len(page["items"]) == 2 and page["next_before"]
    more = authed.get(f"/activity?limit=50&before={page['next_before'].replace('+', '%2B')}").json()["items"]
    assert all(i["at"] < page["next_before"] for i in more)


def test_sessions_list_and_revoke(client):
    from fastapi.testclient import TestClient

    from app.main import app

    login(client, "greg@example.com")
    with TestClient(app) as other:
        login(other, "greg@example.com")
        sessions = client.get("/auth/sessions").json()
        assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
        mine = next(s for s in sessions if s["current"])
        assert client.delete(f"/auth/sessions/{mine['id']}").status_code == 409
        assert client.post("/auth/sessions/revoke-others").status_code == 204
        assert other.get("/auth/me").status_code == 401
        assert client.get("/auth/me").status_code == 200
    with session_factory()() as db:
        assert db.scalars(select(AuditLog.action).where(AuditLog.action == "sessions_revoked")).all() == ["sessions_revoked"]


def test_account_deletion_removes_google_blocks_and_revokes_access(client, google):
    connect(client, google)
    task(client, title="Draft settlement letter", duration_min=60)
    schedule(client)
    write(client)
    assert len(ours(google)) == 1
    r = client.post("/account/delete", json={"confirm_email": "greg.personal@example.com"})
    assert r.status_code == 204, r.text
    assert ours(google) == []  # the block Meridian wrote is gone from Google Calendar
    assert getattr(google, "revoked_tokens", [])  # Meridian's Google access was revoked
    with session_factory()() as db:
        assert db.scalars(select(User)).all() == []
