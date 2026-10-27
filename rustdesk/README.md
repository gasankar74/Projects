# RustDesk unattended access — "Option A" kit

Reach any of your Windows PCs **without anyone sitting at them**, using your own
self-hosted RustDesk server (hbbs + hbbr) and **one shared permanent password**.
No API server, no user accounts — just the free open-source server you already run.

```
 Your PC (technician)                 Your server (TrueNAS, Docker)                 Endpoint PC
 ─────────────────────               ───────────────────────────────               ────────────────────
 RustDesk app  ───── asks "where is ID 123456789?" ──▶ hbbs  (21115-21116 TCP, 21116 UDP)
                                         ◀── endpoint checks in every few seconds ── RustDesk service
 RustDesk app  ◀════ video / mouse / keyboard, end-to-end encrypted ════▶  (direct P2P, or via
                                     hbbr relay (21117 TCP) when P2P can't punch through)
```

The **server key** proves to each PC that it's talking to *your* server.
The **permanent password** is what lets you in without someone clicking "Accept".

---

## What's in this folder

| File | Runs on | What it does |
|---|---|---|
| `truenas/check-rustdesk-server.sh` | **TrueNAS host** (System → Shell or SSH) | Read-only health check: containers, ports, key files, key-only mode, DNS. Prints the public key you need. Changes nothing. |
| `windows/Deploy-RustDesk.cmd` | **Each Windows endpoint** | Double-click launcher for the script below. |
| `windows/Deploy-RustDesk.ps1` | **Each Windows endpoint** | Downloads the official installer, checks its signature, installs silently, sets up the service, points it at your server, locks it to permanent-password-only, asks you for the password, records the ID. |
| `windows/deploy-settings.example.json` | — | Template for your server name + key. Copy to `deploy-settings.json`. |
| `inventory/endpoints.template.csv` | — | Column layout of the inventory the script builds. |

> **This repository is public.** `deploy-settings.json` and `endpoints.csv` are in
> `.gitignore` so your server key, hostnames and endpoint IDs never get committed.
> **Passwords are never written to any file by these scripts.**

---

## One-time prep (about 10 minutes)

### 1. Check the server — TrueNAS host

```bash
sudo bash check-rustdesk-server.sh /mnt/media-pool/appdata/rustdesk rustdesk.example.com
```

What you want to see:

| Check | Good result | If not |
|---|---|---|
| Containers | `hbbs` and `hbbr` **running** | Start the `rustdesk` stack in Portainer |
| Ports | 21115/tcp, 21116/tcp, **21116/udp**, 21117/tcp listening | Stack must use `network_mode: host` or publish these ports |
| Key files | Public key "well-formed" (44 chars ending in `=`), printed on screen | Keys are created on hbbs's first start in the data folder |
| Private key perms | `600` | Warn only. `chmod 600 …/id_ed25519` when convenient |
| Key-only mode | `hbbs requires the key` | **Warn only — don't fix yet.** See "Hardening" step 3 below |
| Summary | `0 failed` | Fix FAILs before deploying |

From a network **outside** your house (phone hotspot), on any Windows PC:

```powershell
Test-NetConnection rustdesk.example.com -Port 21116   # expect TcpTestSucceeded : True
Test-NetConnection rustdesk.example.com -Port 21117   # expect TcpTestSucceeded : True
```

UDP 21116 can't be tested this way; if endpoints later show **offline**, check the
UDP 21116 port-forward on the router first.

### 2. Create the shared password

- Generate a **random 16+ character** password in your password manager.
- No spaces and no `"` (they break command-line passing).
- Never reuse a Windows/email password.
- Save it as e.g. **"RustDesk – unattended endpoint password"**.

### 3. Prepare the USB stick / network share

Copy the `windows` folder to a USB stick or a share every endpoint can reach. Then:

1. Copy `deploy-settings.example.json` → `deploy-settings.json`
2. Fill it in:

```json
{
  "IdServer": "rustdesk.example.com",
  "RelayServer": "rustdesk.example.com",
  "Key": "the 44-character key printed by the TrueNAS check",
  "AllowedIPs": "",
  "ConfigString": ""
}
```

| Field | Meaning |
|---|---|
| `IdServer` | Your hbbs hostname (port 21116 is assumed) |
| `RelayServer` | Your hbbr hostname — usually the same |
| `Key` | Contents of `id_ed25519.pub` |
| `AllowedIPs` | Optional. Comma-separated IPs/CIDRs allowed to connect, e.g. your home WAN IPs. **Leave empty until you're sure your WAN IPs are stable** — a wrong value locks you out |
| `ConfigString` | Optional. The string from RustDesk → Settings → Network → *Export server config*. Only needed if the per-setting method warns on your RustDesk version |

---

## Deploy to each endpoint (about 3 minutes per PC)

1. Plug in the USB stick (or open the share) on the endpoint.
2. Right-click **`Deploy-RustDesk.cmd` → Run as administrator** → Yes on UAC.
3. Watch it work. Each step prints `[OK]`, `[WARN]` or `[FAIL]`:

   | Step | What happens |
   |---|---|
   | Loading settings | Reads `deploy-settings.json` (asks if missing) |
   | Reach your server | DNS lookup + TCP 21116 and 21117. If unreachable it asks before continuing |
   | Find latest release | Asks GitHub for the newest official `x86_64.exe` |
   | Download | Checks SHA-256 (when GitHub lists one) and the **digital signature** — stops if not `Valid` |
   | Install | Silent install to `C:\Program Files\RustDesk` (skipped if already latest) |
   | Service | `Rustdesk` service → Automatic + Running (works at the sign-in screen) |
   | Server + security | Sets server, relay, key, **permanent-password only**, **no Accept click**, optional IP whitelist — then reads each back |
   | Password | **You paste the password twice** (hidden). Not saved anywhere |
   | ID | Prints the RustDesk ID and appends a row to `endpoints.csv` next to the script |

4. The summary shows the **ID**. Write a friendly name in your notes if you like —
   or run with a note: `Deploy-RustDesk.ps1 -Notes "Kitchen PC"`.

Log of each run (without the password): `C:\ProgramData\EBD\RustDesk\deploy-*.log` on the endpoint.

### If a setting shows `[WARN] … did not read back`

Some RustDesk versions don't accept settings from the command line. Fix it once in the
app on that endpoint: **Settings → Security → unlock** and set

- **Password → Use permanent password**
- **Approve mode → Accept sessions via password**

and **Settings → Network → ID/Relay server** (ID server, Relay server, Key).

---

## Set up your own PC(s) (the technician side)

1. Install RustDesk normally (no script needed — you don't want *your* PC unattended).
2. **Settings → Network → ID/Relay server** → same ID server, relay server and key.
3. For each endpoint ID: **Connect** → paste the shared password → tick
   **Remember password** → after it connects, add it to **Favorites** and rename it.

Without an API server, favorites and remembered passwords live **only on that PC**.
Repeat step 3 on your laptop. Keep `endpoints.csv` as the master list.

---

## Test every new endpoint

1. Connect from your PC — should open with no one clicking anything.
2. Reboot the endpoint and **don't sign in**. Connect again — you should see the
   Windows **sign-in screen**. That proves the service works unattended.
3. Look at the session window: **Direct** = peer-to-peer, **Relay** = through your
   server. Both are fine; relay is a bit slower.

| Symptom | Likely cause | Check |
|---|---|---|
| ID shows **offline** | Endpoint can't reach hbbs | UDP/TCP 21116 forward; endpoint's ID server setting |
| "Key mismatch" / "not trusted" | Wrong or missing key on one side | Compare with `id_ed25519.pub` |
| Connects but **black screen** at sign-in | App running, service not | `Get-Service Rustdesk` → Running, Automatic |
| Wrong password | Typo during deploy | Re-run with `-SkipInstall` to set it again |

---

## Hardening — because one password opens every PC

1. **Rotate the password** every 6–12 months, or immediately if a laptop is lost or
   someone who knew it leaves. On each endpoint:
   `Deploy-RustDesk.cmd -SkipInstall` (only re-applies settings + asks for the new password).
   Then update the saved password on your PC(s) and in your password manager.
2. **IP whitelist** (`AllowedIPs`) — only from your known public IPs. Keep a second
   way in (e.g. VPN to the site) before turning it on, so a changed WAN IP can't lock you out.
3. **Key-only server mode** — after **every** endpoint has the key, make hbbs/hbbr
   refuse clients without it (the TrueNAS check tells you if it's on). Turning it on
   too early disconnects any endpoint that doesn't have the key. Check the
   rustdesk-server documentation for the exact flag your image version uses.
4. **Keep RustDesk updated** — re-running the deploy script upgrades to the latest
   release and keeps all settings.
5. **2FA per endpoint** (Settings → Security → 2FA) is available but means a separate
   TOTP code for every PC — usually too much for Option A.

## Moving to Option B later (one login, synced address book)

Add an API server container (e.g. the open-source `lejianwen/rustdesk-api`), then
fill in **API server** on clients. Nothing from Option A has to be undone.
