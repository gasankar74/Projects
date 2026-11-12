# Meridian Planner

An auto-scheduling task manager: you enter tasks, deadlines, priorities and durations; it plans your time around every Google Calendar you use and re-plans when anything changes. Original code and design; behaviour modelled on publicly described features of tools like Motion.

The full plan (requirements, Motion comparison, architecture, schema, scheduling algorithm, Google Calendar plan, milestones, testing, deployment, costs) is in the project's planning doc.

## Status: Phase 5 (all phases built)

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
| Repeating tasks | Fixed schedules (every weekday, every other Friday, the 2nd Tuesday, the last business day of the month, any RRULE) and completion-based ones ("2 weeks after it's done"). Weekend/holiday adjustment to the business day before or after, a due time, a lead window for when work may start, and a missed-occurrence policy (keep open, skip, keep only the newest). Occurrences are ordinary tasks generated ahead of time, idempotently; editing the series updates the ones not yet started. Repeating tasks page, repeat editor with a live preview of the next dates, quick add understands both kinds. |
| Scheduling profiles | Named hours (e.g. Personal: evenings and Saturdays) with an optional daily cap, for a task or a whole category. Holidays remove the default work hours. Energy-aware placement (high-energy work in your peak hours, low-energy work in the slump), travel time around tasks with a location and around calendar events with an address. |
| Near-term optimiser | OR-Tools CP-SAT re-arranges the next 48 hours of flexible work after the constructive pass: honours time-of-day and energy preferences and finishes important work sooner, under every rule (deadlines, dependencies, windows, caps, buffers, locks). Deterministic (single thread, fixed seed, deterministic time budget) and only used when strictly better. |
| Google Calendar write-back | Planned blocks are written to your chosen task calendar (or a task's own calendar), 14 days ahead by default. Each block has a fixed event id, so a retry after a lost response updates instead of duplicating (409 → update). Writes go through an outbox with backoff and a per-minute budget. Confidential tasks appear as a private "Focus block". Read-only calendars are never written. Moving one of these events in Google locks the task there; deleting one recreates it; an hourly reconciliation removes orphans and duplicates. |
| Push notifications | With `MERIDIAN_PUBLIC_URL` set (HTTPS, e.g. Tailscale Funnel), Google push channels per calendar trigger a sync within seconds; tokens are stored hashed and channels renewed before they expire. Without it, the worker polls every 10 minutes and the app re-syncs on focus. |
| AI assistant | Chat page (press **A**) that can find, create, edit, reschedule, complete and delete tasks, read the agenda and risks, and run what-ifs. Runs on Ollama on your own server by default (qwen3:8b); Claude is an optional provider. Every change goes through the same task service and scheduler as the UI, and the reply shows what was actually saved, read back from the database. Deleting, touching more than 5 tasks or moving work to another day shows a preview first and nothing changes until you press Confirm. The scheduler, not the AI, enforces every rule. |
| AI privacy | Confidential tasks are hidden from the assistant unless you turn AI access on for that task, its category or its project; any task, category or project can also be hidden outright. Calendar event titles are not sent to a non-local provider unless you allow it in Settings. |
| Projects | Areas, projects with start dates and deadlines, project pages with a Kanban board (drag to change status and order), a timeline (Gantt) with dependency arrows, milestones and deadlines, and a list. AI breakdown turns a goal into estimated tasks with dependencies, previewed before saving. Templates: save any project as one, three starters (talk, mediation, paper), deadlines counted from the project start. |
| Focus and time | Today page with a daily Top 3 (suggested, or your pick) and the week ahead. Start/stop timer from any task, manual time entries, focus sessions (25/50/90 min). Stopping a timer reduces the remaining time, so the scheduler plans only what's left. |
| Reports | Weekly dashboard, workload by day against working time (by category or project), time by category, project, day and task, CSV export, and estimate accuracy per category. "Plan with learned estimates" (off by default) scales new estimates by each category's median actual/estimate once it has 5 finished tasks (0.5×–2×). |
| Import from Motion or a CSV | Upload a Motion CSV export (or any task CSV); columns are matched automatically and can be changed. A review screen shows every row with its parsed deadline, duration, priority and project, flags duplicates (already imported, same title and deadline as an existing task, repeated in the file), completed tasks and errors, and lets you untick rows. Importing the same file again adds nothing. Projects are created as needed. |
| Notifications | Bell in the app, browser push (Web Push, works on a phone through your Tailscale address) and optional email through Gmail. Alerts for a missed hard deadline, a deadline at risk, an overloaded day, calendar sync failing for 30 minutes or needing reconnection, and optionally 5 minutes before a block starts. Quiet hours, an hourly cap, email only for urgent alerts (throttled), and an optional daily digest. Confidential tasks are never named outside the app. |
| Security | Rate limits, cross-site request checks, a 6 MB request cap, security headers (CSP, no framing), non-root containers, and a production mode that refuses unsafe settings. Activity page with every sign-in, settings change and task change, signed-in sessions you can end. Deleting your account also removes Meridian's blocks from Google Calendar and revokes its Google access. See [docs/security-review.md](docs/security-review.md). |
| Running it | `docker-compose.prod.yml` for your own server, encrypted nightly backups, restore and a monthly restore drill. Runbook: [docs/deployment.md](docs/deployment.md). |
| Data ownership | JSON and CSV export, JSON import with duplicate detection, account deletion. OAuth tokens encrypted at rest (Fernet, rotatable keys). No secrets in the browser bundle. |

Not built: team features (single user by design) and a native mobile app (the web app installs on a phone).

Keyboard: **Q** quick add · **A** assistant · **/** search · **T** today · **D / W / M** day, week, month · **← →** previous/next · **G** planner.

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

### AI assistant

The assistant uses [Ollama](https://ollama.com) by default, so nothing leaves your server.

```bash
ollama pull qwen3:8b                 # then, in backend/.env (defaults shown):
MERIDIAN_AI_PROVIDER=ollama
MERIDIAN_OLLAMA_URL=http://localhost:11434
```

Under Docker Compose the API reaches Ollama on the host at `host.docker.internal:11434`. To use Claude instead, set `MERIDIAN_AI_PROVIDER=claude` and `MERIDIAN_ANTHROPIC_API_KEY`; the key stays on the server. `MERIDIAN_AI_PROVIDER=none` turns the assistant off. Without a model, `python -m scripts.fake_ollama_server` (port 11435) is a rule-based stand-in that understands "move the board memo to Friday" and "what's on today", for demos and tests.

## Tests

```bash
cd backend && python -m pytest -q        # needs the meridian_test database
cd backend && python -m pytest -q -m acceptance   # just the brief's 15 acceptance tests
cd web && npx tsc --noEmit && npm run build
```

Covered now: acceptance tests 1 (task placed before its deadline), 2 (a new meeting moves the task, not the meeting), 3 (urgent work displaces lower priority), 5 (impossible hard deadline alerts, never placed late), 6 (dependencies respected), 7 (partial completion schedules only the remaining time), 10 (locked blocks kept), 12 (overload detected with a deferral suggestion), 14 (complete → history), 8 (overlapping events across calendars and accounts merge into one busy span), 9 (sync failure, retry and token-expiry recovery without duplicates), 13 (events in other zones and across DST stored as correct instants), plus read-only calendar protection, user isolation, dependency loops, export/import de-duplication, OAuth state checking, and the quick-add examples from the brief. 13 is also checked end to end through the scheduler. Phase 3 adds 4 (completing a repeating task generates the next one), 9 on the write side (a lost response and a retry never duplicate an event; reconciliation removes duplicates) and 11 (last business day, with weekends and holidays). Phase 4 adds 15 (the assistant reschedules a task; after Confirm the change is in the database, the plan and the Google Calendar event), plus the confirmation rules, privacy masking, project templates and breakdowns, and time tracking. Phase 5 adds Motion/CSV import with duplicate detection, notifications and their throttling, the security fixes, and a performance test (500 tasks in an overloaded six weeks re-plan in under 5 seconds; about 1.3 s now).

## Layout

```
backend/   FastAPI app (app/), Alembic migrations, tests, fake Google server (scripts/)
web/       Next.js app (app/ pages, components/, lib/)
docker-compose.yml  (CI: ../.github/workflows/meridian-planner.yml)
```
