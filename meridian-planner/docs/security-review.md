# Security review (Phase 5)

Date: 2026-11-12. Scope: the API (FastAPI), worker, web app (Next.js), Docker Compose deployment, backups, and the data that leaves the server (Google, browser push, email, AI). Method: a read-through of every router and service, automated tests for each fix (`tests/test_hardening.py`, `tests/test_notifications.py`, `tests/test_import.py`), a route-by-route authentication check, a dependency audit, and a scan of the built browser bundle.

## Fixed in this phase

| # | Finding | Risk | Fix |
| --- | --- | --- | --- |
| 1 | A replan of 500 tasks in an overloaded week took **62 seconds** (free-time rebuild was quadratic; the overload advice re-ran the whole plan up to ~24 times). One large import could stall the worker and every what-if request. | Availability | Free time is rebuilt in one sweep (checked against the old code on 3,000 random cases) and prerequisites are ordered with a heap, so plans are unchanged. The overload advice now only re-plans once the tasks it would defer free at least the missing time. All scheduler tests pass; now **1.3 s**. `tests/test_performance.py` keeps it under 5 s. |
| 2 | No rate limits. | Abuse, cost | Per address: sign-in 20/min. Per user: assistant and AI breakdowns 15/min and 200/hour, imports 10/min, exports 20/min, test emails 5 per 5 min, account deletion 5/hour, other writes 300/min, reads 1200/min. `429` with `Retry-After`. |
| 3 | Cross-site request forgery relied only on `SameSite=Lax` cookies. | Integrity | State-changing requests from another site (`Origin` not the app, or `Sec-Fetch-Site: cross-site`) are refused with 403. Google's webhook is exempt (it authenticates with a per-channel secret). |
| 4 | The web app sent no security headers. | XSS, clickjacking | Content-Security-Policy (same-origin only, no framing, no plugins), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, HSTS in production. The API adds `nosniff`, `DENY` and `Cache-Control: no-store`. |
| 5 | Containers ran as root. | Container escape impact | API/worker run as uid 10001, the web app as `node`. |
| 6 | The Compose file published Postgres (password `meridian`) and the API on every network interface. | Data exposure on the LAN | `docker-compose.prod.yml`: Postgres not published; API and web bound to `127.0.0.1`; database password required from `.env`. |
| 7 | Development email sign-in could be left on in production. | Account takeover | In production the API refuses to start with dev login on, non-secure cookies, a non-HTTPS app address, or no encryption key. |
| 8 | Deleting the account left Meridian's task blocks in Google Calendar and its Google access in place. | Data left behind | Deletion removes the blocks Meridian wrote (never your own events) and revokes the Google tokens, then deletes everything. |
| 9 | No way to see or end other sessions; expired sessions were never deleted. | Lost device | Activity page lists sessions; sign out one or all others; expired sessions are purged hourly. |
| 10 | Request bodies were unbounded. | Memory exhaustion | 6 MB cap (413); CSV imports limited to 5 MB and 5,000 rows. |
| 11 | The HTTP client logged every Google request URL at INFO (calendar ids, sync tokens). | Data in logs | Lowered to WARNING. Logs can be JSON (`MERIDIAN_LOG_FORMAT=json`), rotated by Docker. |
| 12 | New channels leave the server: browser push goes through the browser vendor's push service and email through Gmail. | Confidentiality | Confidential tasks are never named outside the app (“a confidential task”), in alerts and the digest. Push payloads are end-to-end encrypted (Web Push); subscription endpoints must be HTTPS and are dropped when the browser says they're gone. |
| 13 | Audit trail missed sign-ins, sign-outs and settings changes. | Accountability | Now recorded, and shown with task history (by you, the assistant or the scheduler) on the Activity page. |

## Checked, no change needed

- **Authentication on every route.** Of 120 API routes, only six are public, all by design: Google sign-in start and callback, dev sign-in (off in production), sign-in configuration, sign-out, and Google's webhook.
- **Isolation between users.** Every query filters by the signed-in user; tests confirm another user gets 404 for tasks, imports and notifications.
- **OAuth.** PKCE (S256), HMAC-signed state with a 10-minute expiry, verifier in an HttpOnly cookie. Google tokens are encrypted at rest (Fernet, rotatable keys).
- **Sessions.** 256-bit random tokens stored only as SHA-256 hashes; HttpOnly, SameSite=Lax, Secure in production; 30 days.
- **Injection.** All SQL goes through SQLAlchemy parameters; the two raw statements take no user input or bind it. Task links accept only `http(s)` URLs, so no `javascript:` links.
- **Secrets in the browser.** The built bundle contains no secret values (scanned for the configured keys); the browser only talks to `/api`, which the web server proxies.
- **Google webhook.** Channel tokens are stored hashed and compared in constant time; the response never reveals whether a channel exists.
- **Read-only calendars** are never written, whatever the stored access role says (tested in Phase 3).
- **AI.** Deletes, changes to more than 5 tasks, and moves to another day need your Confirm, enforced in code; the AI can't bypass the scheduler's rules. Confidential tasks are hidden by default. A prompt-injection in a task's text can at most propose changes you then see and confirm.
- **Dependencies.** `pip-audit` and `npm audit` (production dependencies): no known vulnerabilities on 2026-11-12. CI runs the full test suite on every push.

## Remaining risks and recommendations

- **Tailscale is the perimeter.** Keep the app on `tailscale serve` (tailnet only). If you use Funnel for Google's webhook, publish only that path.
- **Rate-limit counters are in memory.** They reset when the API restarts, and assume one API process (how Compose runs it).
- **CSP allows inline scripts**, which Next.js needs for hydration. A nonce-based policy is possible later.
- **Backups hold the whole database** (Google tokens inside are still encrypted with the app key). Keep the backup passphrase and `.env` files in a password manager, not next to the backups.
- **The Gmail app password** sits in `backend/.env` (mode 600). Revoke it in your Google account if the server is ever compromised.
- **Single user by design.** Team features stay switched off; turning them on would need a review of sharing rules first.
