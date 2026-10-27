#!/usr/bin/env bash
# check-rustdesk-server.sh — READ-ONLY health check for a self-hosted RustDesk
# server (hbbs + hbbr) running in Docker on TrueNAS SCALE.
#
# Run ON THE TRUENAS HOST (System > Shell, or SSH), not inside a container:
#   sudo bash check-rustdesk-server.sh [DATA_DIR] [PUBLIC_HOSTNAME]
#
#   DATA_DIR         folder mounted as /root in the hbbs/hbbr containers
#                    (default: /mnt/media-pool/appdata/rustdesk)
#   PUBLIC_HOSTNAME  optional, e.g. rustdesk.example.com — checks DNS vs WAN IP
#
# It changes NOTHING: no restarts, no config edits, no firewall changes.

set -u

DATA_DIR="${1:-/mnt/media-pool/appdata/rustdesk}"
PUBLIC_HOST="${2:-}"

PASS=0; WARN=0; FAIL=0
if [ -t 1 ]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'; else G=; Y=; R=; C=; N=; fi
ok()   { echo "  ${G}[PASS]${N} $*"; PASS=$((PASS+1)); }
warn() { echo "  ${Y}[WARN]${N} $*"; WARN=$((WARN+1)); }
bad()  { echo "  ${R}[FAIL]${N} $*"; FAIL=$((FAIL+1)); }
info() { echo "         $*"; }
hdr()  { echo; echo "${C}== $* ==${N}"; }

echo "RustDesk server check — $(hostname) — $(date '+%Y-%m-%d %H:%M')"

# ---------------------------------------------------------------------------
hdr "1. Prerequisites"
if [ "$(id -u)" -ne 0 ]; then
  bad "Not running as root. Re-run with: sudo bash $0 $*"
  exit 1
fi
ok "Running as root"
if ! command -v docker >/dev/null 2>&1; then
  bad "docker command not found — is this the TrueNAS host?"
  exit 1
fi
ok "docker is available ($(docker --version 2>/dev/null | cut -d, -f1))"

# ---------------------------------------------------------------------------
hdr "2. Containers"
HBBS=""; HBBR=""
while IFS= read -r name; do
  [ -z "$name" ] && continue
  spec="$(docker inspect --format '{{.Name}} {{.Config.Image}} {{json .Config.Entrypoint}} {{json .Config.Cmd}}' "$name" 2>/dev/null)"
  case "$spec" in *rustdesk*|*hbbs*|*hbbr*) ;; *) continue ;; esac
  if echo "$spec" | grep -q 'rustdesk-server-s6'; then HBBS="$name"; HBBR="$name"; continue; fi
  echo "$spec" | grep -q 'hbbs' && HBBS="$name"
  echo "$spec" | grep -q 'hbbr' && HBBR="$name"
done < <(docker ps -a --format '{{.Names}}')

for role in hbbs hbbr; do
  if [ "$role" = hbbs ]; then c="$HBBS"; else c="$HBBR"; fi
  if [ -z "$c" ]; then bad "$role container not found"; continue; fi
  st="$(docker inspect --format '{{.State.Status}}' "$c")"
  rs="$(docker inspect --format '{{.RestartCount}}' "$c")"
  nm="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$c")"
  img="$(docker inspect --format '{{.Config.Image}}' "$c")"
  if [ "$st" = running ]; then ok "$role → container '$c' is running (image $img, network $nm, restarts $rs)"
  else bad "$role → container '$c' is '$st'"; fi
  [ "$rs" -gt 5 ] 2>/dev/null && warn "$role has restarted $rs times — check: docker logs --tail 50 $c"
done

# ---------------------------------------------------------------------------
hdr "3. Listening ports on this host"
# Expected: 21115/tcp NAT test, 21116/tcp+udp ID/rendezvous, 21117/tcp relay.
# 21118/21119 are only for the web client and are optional.
check_port() { # proto port label required
  local flag; [ "$1" = tcp ] && flag="-lnt" || flag="-lnu"
  if ss -H "$flag" "sport = :$2" 2>/dev/null | grep -q .; then ok "$2/$1 listening ($3)"
  elif [ "$4" = yes ]; then bad "$2/$1 NOT listening ($3)"
  else info "$2/$1 not listening ($3 — optional)"; fi
}
if command -v ss >/dev/null 2>&1; then
  check_port tcp 21115 "hbbs NAT type test" yes
  check_port tcp 21116 "hbbs ID/rendezvous" yes
  check_port udp 21116 "hbbs ID/rendezvous" yes
  check_port tcp 21117 "hbbr relay" yes
  check_port tcp 21118 "hbbs web client" no
  check_port tcp 21119 "hbbr web client" no
else
  warn "'ss' not available — skipping port check"
fi

# ---------------------------------------------------------------------------
hdr "4. Server key files in $DATA_DIR"
PUB=""
if [ ! -d "$DATA_DIR" ]; then
  bad "$DATA_DIR does not exist — pass the right folder as the first argument"
else
  if [ -f "$DATA_DIR/id_ed25519.pub" ]; then
    PUB="$(tr -d '[:space:]' < "$DATA_DIR/id_ed25519.pub")"
    if echo "$PUB" | grep -Eq '^[A-Za-z0-9+/]{43}=$'; then ok "Public key present and well-formed"
    else warn "Public key present but looks unusual (length ${#PUB})"; fi
    info "Public key (paste into deploy-settings.json → \"Key\"):"
    info "  $PUB"
  else
    bad "id_ed25519.pub missing — clients can't verify the server"
  fi
  if [ -f "$DATA_DIR/id_ed25519" ]; then
    perm="$(stat -c '%a' "$DATA_DIR/id_ed25519")"
    case "$perm" in
      600|400) ok "Private key present, permissions $perm" ;;
      *) warn "Private key permissions are $perm (others can read it). Suggested later: chmod 600 $DATA_DIR/id_ed25519" ;;
    esac
  else
    bad "Private key id_ed25519 missing"
  fi
fi

# ---------------------------------------------------------------------------
hdr "5. Key-only (encrypted-only) mode"
# Without it, anyone who knows your hostname can use your relay without the key.
if [ -n "$HBBS" ]; then
  cfg="$(docker inspect --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}} {{json .Config.Env}}' "$HBBS")"
  if echo "$cfg" | grep -Eq '"-k"|"-k _"|-k _|ENCRYPTED_ONLY=1'; then
    ok "hbbs requires the key (found -k / ENCRYPTED_ONLY=1)"
  else
    warn "hbbs does NOT appear to require the key."
    info "Fix later (after EVERY endpoint has the key, or they'll stop connecting):"
    info "  add  command: hbbs -k _  (or env ENCRYPTED_ONLY=1) to hbbs, and  hbbr -k _  to hbbr."
  fi
  # Does the key hbbs logged match the file?
  logkey="$(docker logs "$HBBS" 2>&1 | grep -Eo 'Key: [A-Za-z0-9+/]{43}=' | tail -1 | cut -d' ' -f2)"
  if [ -n "$logkey" ] && [ -n "$PUB" ]; then
    if [ "$logkey" = "$PUB" ]; then ok "Key in hbbs log matches id_ed25519.pub"
    else bad "Key in hbbs log differs from $DATA_DIR/id_ed25519.pub — container may mount a different folder"; fi
  else
    info "Couldn't find a 'Key:' line in hbbs logs to compare (normal if logs were rotated)."
  fi
fi

# ---------------------------------------------------------------------------
if [ -n "$PUBLIC_HOST" ]; then
  hdr "6. Public hostname $PUBLIC_HOST"
  dns="$(getent ahostsv4 "$PUBLIC_HOST" | awk 'NR==1{print $1}')"
  wan="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
  if [ -z "$dns" ]; then bad "$PUBLIC_HOST does not resolve"
  else info "$PUBLIC_HOST resolves to $dns (from this host's DNS)"; fi
  if [ -n "$wan" ]; then
    info "This site's public (WAN) IP is $wan"
    if [ "$dns" = "$wan" ]; then ok "Public DNS points at this site"
    elif echo "$dns" | grep -Eq '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'; then
      info "Local DNS returns a LAN IP (split DNS) — fine inside the LAN; outside, public DNS must point at $wan."
    else warn "DNS ($dns) ≠ WAN IP ($wan) — remote endpoints may not find the server"; fi
  else
    info "Couldn't look up the WAN IP (no internet from the host?)."
  fi
  info "Port forwards can't be proven from inside the LAN. From an OUTSIDE network run:"
  info "  Test-NetConnection $PUBLIC_HOST -Port 21116   and   -Port 21117   (expect TcpTestSucceeded : True)"
fi

# ---------------------------------------------------------------------------
hdr "Summary"
echo "  ${G}$PASS passed${N}, ${Y}$WARN warnings${N}, ${R}$FAIL failed${N}"
[ "$FAIL" -eq 0 ] || exit 2
exit 0
