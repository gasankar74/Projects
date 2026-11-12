# Deploying Meridian Planner on your own server

This runbook sets Meridian up on a home server (written for Cerybros: Ubuntu, Docker, Ollama already running) and covers updates, backups, restores and recovery. Everything runs in Docker Compose; Tailscale gives you an HTTPS address that only your own devices can reach. Running costs: $0.

## 1. First install

You need Docker with the Compose plugin, Tailscale, git, gpg, and Ollama with `qwen3:8b` pulled (`ollama pull qwen3:8b`).

```bash
sudo mkdir -p /opt/meridian-planner && sudo chown "$USER" /opt/meridian-planner
git clone https://github.com/gasankar74/Projects.git /tmp/projects
cp -r /tmp/projects/meridian-planner/. /opt/meridian-planner/
cd /opt/meridian-planner
```

### HTTPS address (Tailscale)

```bash
sudo tailscale serve --bg 3000          # https://<machine>.<tailnet>.ts.net -> the web app
tailscale serve status                   # shows the address; use it below as APP_URL
```

Only devices signed in to your tailnet can open it. Install Tailscale on your phone and laptop to use Meridian away from home.

### Or: your own domain through Nginx Proxy Manager

If you already run Nginx Proxy Manager (NPM) with a domain, use it instead of `tailscale serve`:

1. Put `MERIDIAN_WEB_BIND=<server LAN address>` (and `MERIDIAN_WEB_PORT=3001` if 3000 is taken) in the top-level `.env`, so the proxy can reach the web app.
2. DNS: an `A` record for the name (e.g. `planner.example.net`) pointing at your public IP, with port 443 forwarded to NPM. For a private-only address, point it at a LAN or Tailscale address and use a DNS challenge instead.
3. NPM, **Proxy Hosts > Add Proxy Host**: the domain name; scheme `http`; forward host = the LAN address; port = the web port; tick **Block Common Exploits**. **SSL** tab: request a new Let's Encrypt certificate, tick **Force SSL**, **HTTP/2** and **HSTS**. **Advanced** tab: `client_max_body_size 6m;` (CSV imports).
4. APP_URL is then `https://planner.example.net`. Only the web app goes through the proxy; the API and database stay unreachable from the network.

A public address means the sign-in page is on the internet: `MERIDIAN_ALLOWED_EMAILS` (below, required in production) decides who can get in, and sign-in attempts are rate limited. Google's instant calendar updates can then use `MERIDIAN_PUBLIC_URL=https://planner.example.net/api`.

### Settings

```bash
cp .env.example .env                     # Compose: the database password
openssl rand -base64 32 | tr -d '\n/+=' # paste as MERIDIAN_DB_PASSWORD in .env
cp backend/.env.example backend/.env     # the app's settings
chmod 600 .env backend/.env
```

In `backend/.env` set:

| Setting | Value |
| --- | --- |
| `MERIDIAN_TOKEN_ENCRYPTION_KEYS` | output of `docker compose run --rm api python -m app.cli genkey` |
| `MERIDIAN_FRONTEND_URL` | APP_URL |
| `MERIDIAN_GOOGLE_REDIRECT_URI` | APP_URL + `/api/auth/google/callback` |
| `MERIDIAN_GOOGLE_CLIENT_ID`, `..._SECRET` | from Google Cloud (below) |
| `MERIDIAN_ALLOWED_EMAILS` | the Google addresses allowed to sign in, comma-separated (see Users) |
| `MERIDIAN_DEV_LOGIN_ENABLED` | `false` (production forces this anyway) |
| `MERIDIAN_VAPID_PUBLIC_KEY`, `..._PRIVATE_KEY` | output of `docker compose run --rm api python -m app.cli vapid` (browser notifications) |
| `MERIDIAN_SMTP_HOST` etc. | optional, for email alerts and the daily digest (below) |

Production mode refuses to start if no allowed addresses are listed, dev login is on, cookies aren't marked secure, the app address isn't HTTPS, or the encryption key is missing. The production Compose file sets `MERIDIAN_ENV=production`, secure cookies and JSON logs for you.

**Google Cloud.** Create an OAuth client (Web application) with the redirect URI above, enable the Google Calendar API, and set the OAuth consent screen's publishing status to **In production** (unverified is fine for personal use; in Testing mode Google expires calendar access every 7 days).

**Email (optional).** For Gmail: turn on 2-Step Verification, create an app password, and set

```
MERIDIAN_SMTP_HOST=smtp.gmail.com
MERIDIAN_SMTP_PORT=587
MERIDIAN_SMTP_USER=you@gmail.com
MERIDIAN_SMTP_PASSWORD=<16-character app password>
MERIDIAN_EMAIL_FROM=you@gmail.com
```

**Instant calendar updates (optional).** Google can tell Meridian about calendar changes within seconds, but it needs a public HTTPS address for `/webhooks/google`. Tailscale Funnel can publish just that path on another port (check `tailscale funnel --help` for your version):

```bash
sudo tailscale funnel --bg --https=8443 --set-path=/webhooks/google http://127.0.0.1:8000/webhooks/google
```

then set `MERIDIAN_PUBLIC_URL=https://<machine>.<tailnet>.ts.net:8443`. If Google rejects the address, leave `MERIDIAN_PUBLIC_URL` empty: calendars are then checked every 10 minutes and whenever you open the app.

### Start

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps        # all "healthy" after a minute
curl -s http://127.0.0.1:8000/healthz                                       # {"ok":true,"db":"ok","worker_ok":true,...}
```

Database migrations run automatically when the API starts. Open APP_URL, sign in with Google, then in **Calendars & settings** choose your task calendar, and turn on browser notifications (and the digest, if you set up email).

Tip: `alias mp='docker compose -f /opt/meridian-planner/docker-compose.yml -f /opt/meridian-planner/docker-compose.prod.yml'`.

### Users

Each person gets their own separate account: their own tasks, calendars, plan, assistant and settings; nobody sees anyone else's data. There is no shared workspace.

- **Add someone:** add their Google address to `MERIDIAN_ALLOWED_EMAILS` in `backend/.env`, run `mp up -d`, and send them the address. They sign in with Google, then connect their calendars.
- **One person, several Google accounts** (personal and work): list only the main address. After signing in, connect the others in **Calendars & settings > Connect Google account**; those accounts can then also be used to sign in, into the same Meridian account.
- **Remove someone:** take their address off the list and `mp up -d`: they can't sign in again and their open sessions stop working; they (or you, signed in as them) can delete the account from Settings, which also removes Meridian's blocks from their Google Calendar.
- **Google's side:** a personal OAuth app that hasn't been verified by Google shows an "unverified app" warning (click Advanced > continue) and is limited to 100 users, which is plenty.

## 2. Updating

```bash
cd /opt/meridian-planner
deploy/backup.sh                              # a backup first, always
git -C /tmp/projects pull && cp -r /tmp/projects/meridian-planner/. .
mp up -d --build                              # migrations run on start
mp ps && curl -s http://127.0.0.1:8000/healthz
```

To roll back: check out the previous version, `mp up -d --build`, and if the database schema changed, restore the backup you just took (section 4).

## 3. Backups

`deploy/backup.sh` writes an encrypted (AES-256, gpg) PostgreSQL dump to `/srv/backups/meridian`, checks that it decrypts and lists its tables, and deletes backups older than 30 days.

```bash
sudo mkdir -p /srv/backups/meridian && sudo chown "$USER" /srv/backups/meridian   # ideally a second disk or a NAS mount
mkdir -p ~/.config/meridian && openssl rand -base64 32 > ~/.config/meridian/backup-passphrase && chmod 600 ~/.config/meridian/backup-passphrase
deploy/backup.sh
```

**Keep a copy of the passphrase and of both `.env` files somewhere off this machine** (a password manager). Without the passphrase the backups can't be read; without `MERIDIAN_TOKEN_ENCRYPTION_KEYS` a restored database can't use its Google tokens (you'd reconnect your Google accounts, nothing else is lost).

Nightly at 02:30 with systemd:

```bash
sudo cp deploy/meridian-backup.service deploy/meridian-backup.timer /etc/systemd/system/
sudoedit /etc/systemd/system/meridian-backup.service    # set User= to your user
sudo systemctl daemon-reload && sudo systemctl enable --now meridian-backup.timer
systemctl list-timers meridian-backup.timer
journalctl -u meridian-backup.service                   # results
```

Settings (environment variables): `MERIDIAN_BACKUP_DIR`, `MERIDIAN_BACKUP_PASSFILE`, `MERIDIAN_BACKUP_KEEP_DAYS`.

## 4. Restoring

```bash
deploy/restore.sh latest --into meridian_check     # into a separate database, to look before replacing anything
deploy/restore.sh /srv/backups/meridian/meridian-20261112T023000Z.dump.gpg --live
```

`--live` asks you to type the database name, stops the API and worker, keeps the current database as `meridian_before_restore`, restores, and starts them again. Afterwards the worker re-syncs calendars and replans on its own.

### Restore drill (monthly)

```bash
deploy/restore-drill.sh
```

It takes a fresh backup, restores it into a scratch database, compares every table's row count and the schema version with the live database, drops the scratch copy, and appends PASS or FAIL to `/srv/backups/meridian/restore-drills.log`. It exits non-zero on failure, so it can run from a timer too.

## 5. Other recovery tasks

**Re-sync every calendar from scratch** (after a long outage, or if events look wrong): in Calendars & settings press **Sync now**; if that doesn't clear it, disconnect and reconnect the Google account (tasks are kept), or run `mp exec api python -m app.cli sync`.

**Rotate the encryption key.** Generate a new key and put it first: `MERIDIAN_TOKEN_ENCRYPTION_KEYS=<new>,<old>`, then `mp up -d`. New tokens are encrypted with the new key, old ones still decrypt. Once every Google account has refreshed (an hour), the old key can be removed.

**Move to new hardware.** On the new machine do section 1 with the same `.env` files (same encryption key), copy the latest backup and the passphrase across, start only the database (`mp up -d db`), run `deploy/restore.sh <file> --live`, then `mp up -d`. Point Tailscale serve at it, and update the Google redirect URI if the address changed.

**Something's wrong.** `mp ps` (health), `mp logs --tail=200 api worker` (JSON logs), `curl -s 127.0.0.1:8000/healthz` (database and worker heartbeat). The app shows a banner when calendar sync fails, and after 30 minutes of failures you get a notification (and an email, if set up).
