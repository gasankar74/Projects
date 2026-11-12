# Meridian Planner

An auto-scheduling task manager: you enter tasks, deadlines, priorities and durations; it plans your time around every Google Calendar you use and re-plans when anything changes. Original code and design; behaviour modelled on publicly described features of tools like Motion.

The full plan (requirements, Motion comparison, architecture, schema, scheduling algorithm, Google Calendar plan, milestones, testing, deployment, costs) is in the project's planning doc.

## Status: Phase 2 (scheduling engine)

| Area | What works now |
| --- | --- |
| Sign-in | Google OAuth (PKCE, signed state), server-side sessions in HTTP-only cookies. Optional local email sign-in for development. |
| Database | PostgreSQL schema with Alembic migrations: users, sessions, Google accounts, calendars, events, categories, projects, tasks, checklists, dependencies, attachments, task change history, audit log. |
| Master task list | Every task field from the brief (hard/soft deadline, preferred date, earliest start, duration and remaining time, split rules, min/max session, windows, allowed days, buffers, pinned/locked, energy, tags, links, confidential flag, recurrence rule). Inbox, categories (8 defaults), projects, filters, search, sorting, overdue highlighting, completion to history, reopen, soft delete, per-task change log, dependencies with loop detection. |
| Quick add | Offline natural-language parser: dates ("by Friday", "tomorrow", "Nov 20 at 3pm"), durations, priority, recurrence ("every Friday", "last weekday of every month"), sessions ("90 minutes three times this week"), tags, confidential. Live preview before saving. |
| Google Calendar | Connect several Google accounts; reads every calendar with its colour and access level; incremental sync with sync tokens; cancellations, recurring instances, all-day events, time zones and DST; free/declined events don't block time; read-only calendars can never be chosen for writing; failures are recorded per calendar and recovered on the next sync with no duplicate events; revoked access shows "Reconnect". Merged busy-time endpoint (`/busy`) for the scheduler. |
| Web app | Planner page (calendar day/week/month + prioritised task panel), task list with filters, task editor drawer, quick add (press **Q**), settings for calendars, time zone, categories, export/import, account deletion. Dark mode, keyboard shortcuts, mobile layout, installable (PWA manifest). |
| Scheduling engine | Deterministic, explainable planner (`app/scheduler/engine.py`): fits tasks into free time across all calendars, by priority, deadline urgency, category, energy and age; respects work hours, windows, allowed days, buffers, transition time, a daily cap, earliest start, dependencies, split rules and min/max sessions. Hard deadlines are never scheduled late: it moves less pressing work, or raises a "cannot meet deadline" alert. Locked and pinned blocks never move; blocks about to start are frozen. Stable re-plans (blocks only move when there's a real gain), overload detection with a suggested set of tasks to defer, at-risk warnings and a "why is this here" explanation for every block. |
| Re-planning | Any task change, calendar sync, time-zone change or calendar setting marks the plan stale; a background worker re-plans within seconds. Every plan is saved, so **Undo** restores the previous one. What-if endpoint for previewing changes without saving. |
| Planner UI | Planned blocks on the calendar in category colour, "Why it's here" card with lock/unlock, plan status (free time, overload, warnings), Replan and Undo, per-task Schedule tab, Priority matrix page, scheduling settings (work hours, transition time, daily cap, session length, horizon). |
| Data ownership | JSON and CSV export, JSON import with duplicate detection, account deletion. OAuth tokens encrypted at rest (Fernet, rotatable keys). No secrets in the browser bundle. |

Not yet (later phases): writing task blocks to Google and push webhooks (Phase 3), recurrence series generation (Phase 3), AI assistant (Phase 4), Motion import and notifications (Phase 5).

Keyboard: **Q** quick add · **/** search · **T** today · **D / W / M** day, week, month · **← →** previous/next · **G** planner.

## Run it locally

Requirements: Python 3.12+, Node 22, PostgreSQL 16.

```bash
# database
createuser -s meridian; createdb -O meridian meridian; createdb -O meridian meridian_test   # or: docker compose up db

# API
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python -m app.cli genkey            # paste into MERIDIAN_TOKEN_ENCRYPTION_KEYS in .env
alembic upgrade head
uvicorn app.main:app --port 8000
python -m app.cli worker           # second terminal: background re-planning and calendar sync

# web app (second terminal)
cd web
npm install
npm run dev                         # http://localhost:3000
```

### Connect real Google Calendars

1. In [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Google Calendar API**.
2. OAuth consent screen: External, Testing mode; add your Google addresses as test users (no verification needed while testing).
3. Scopes: `openid`, `email`, `profile`, `.../auth/calendar.readonly`, `.../auth/calendar.events`.
4. Credentials > Create OAuth client ID > Web application. Authorized redirect URI: `http://localhost:3000/api/auth/google/callback` (and your production URL later).
5. Put the client ID and secret in `backend/.env`, restart the API, and use **Continue with Google**. Add more accounts from **Calendars & settings > Connect Google account**.

### Try it without Google

A local stand-in for Google's OAuth and Calendar APIs ships for demos and end-to-end tests. It seeds a personal and a law-firm account with overlapping events this week.

```bash
cd backend && python -m scripts.fake_google_server     # :8765
# then add to backend/.env:
MERIDIAN_GOOGLE_CLIENT_ID=fake
MERIDIAN_GOOGLE_CLIENT_SECRET=fake
MERIDIAN_GOOGLE_AUTH_URL=http://localhost:8765/o/oauth2/v2/auth
MERIDIAN_GOOGLE_TOKEN_URL=http://localhost:8765/token
MERIDIAN_GOOGLE_USERINFO_URL=http://localhost:8765/userinfo
MERIDIAN_GOOGLE_CALENDAR_API=http://localhost:8765/calendar/v3
```

## Tests

```bash
cd backend && python -m pytest -q        # needs the meridian_test database
cd web && npx tsc --noEmit && npm run build
```

Covered now: acceptance tests 1 (task placed before its deadline), 2 (a new meeting moves the task, not the meeting), 3 (urgent work displaces lower priority), 5 (impossible hard deadline alerts, never placed late), 6 (dependencies respected), 7 (partial completion schedules only the remaining time), 10 (locked blocks kept), 12 (overload detected with a deferral suggestion), 14 (complete → history), 8 (overlapping events across calendars and accounts merge into one busy span), 9 (sync failure, retry and token-expiry recovery without duplicates), 13 (events in other zones and across DST stored as correct instants), plus read-only calendar protection, user isolation, dependency loops, export/import de-duplication, OAuth state checking, and the quick-add examples from the brief. 13 is also checked end to end through the scheduler. Tests 4, 11 (recurrence) and 15 (AI rescheduling) belong to Phases 3 and 4.

## Layout

```
backend/   FastAPI app (app/), Alembic migrations, tests, fake Google server (scripts/)
web/       Next.js app (app/ pages, components/, lib/)
docker-compose.yml  (CI: ../.github/workflows/meridian-planner.yml)
```
