#!/usr/bin/env bash
# =============================================================================
# yral-chat-ai — bootstrap Caddy auto-start on rishi-1, rishi-2, rishi-3
# =============================================================================
#
# WHAT THIS SCRIPT DOES (one-time setup, run from operator's Mac):
#
# For each Hetzner host:
#   1. scp the updated caddy/render-caddy-compose.sh → /home/deploy/caddy/
#      (changes Caddy's restart policy from `unless-stopped` to `always`).
#   2. Re-render docker-compose.yml on the host with the new policy and
#      `docker compose up -d` Caddy to pick it up. Idempotent: if Caddy
#      is already running with the new compose, this is a no-op.
#   3. Install an `@reboot` entry in the deploy user's own crontab that
#      runs `cd /home/deploy/caddy && docker compose up -d` on every boot.
#
# WHY THIS SCRIPT EXISTS:
#
# 2026-05-18 incident: after Saikat's monthly server reboot, Caddy stayed
# DOWN on rishi-2 and rishi-3, causing roughly 2/3 of Cloudflare probes
# to surface as 521. Root cause: Caddy was stopped 12–14 minutes BEFORE
# each reboot by an opaque pre-shutdown step (likely an apt upgrade
# bouncing the docker daemon, or an explicit `docker stop`). Docker's
# `unless-stopped` restart policy treats that as "user stopped it" and
# refuses to bring Caddy back on the next boot. rishi-1 happened to be
# untouched and was stopped only at reboot time, which `unless-stopped`
# DOES recover from — explaining why only rishi-1 came back.
#
# Two complementary fixes:
#   - `restart: always` (in render-caddy-compose.sh) — Caddy comes back
#     even after a manual `docker stop`, mid-day daemon restart, or
#     SIGTERM. Only `docker rm caddy` or `docker compose down` can keep
#     it down between reboots.
#   - `@reboot` cron — on every boot, deploy user reruns
#     `docker compose up -d` from /home/deploy/caddy/. Catches the case
#     where Caddy was `docker compose down`'d (container removed) before
#     a reboot, which `restart: always` cannot recover from on its own.
#
# Together: Caddy survives ANY combination of reboot + manual stop +
# compose down, as long as the /home/deploy/caddy/ directory itself
# remains intact.
#
# WHY CRON NOT SYSTEMD:
#
# Installing a system-wide systemd unit under /etc/systemd/system/
# requires root access. The deploy user on the Hetzner hosts does NOT
# have passwordless sudo (only Saikat holds the sudo password). cron
# @reboot is the well-worn equivalent: runs at boot under the deploy
# user's identity, no elevated privileges needed. deploy is already in
# the `docker` group (for normal CI deploys), so `docker compose up -d`
# Just Works without sudo.
#
# Limitation: cron @reboot fires when the `cron` service starts, which
# is not strictly ordered after `docker.service`. The 30-second sleep
# before the docker call is a coarse but reliable workaround on these
# hosts. If we ever see "Cannot connect to the Docker daemon" in
# /home/deploy/caddy-autostart.log, bump the sleep or add a wait loop.
#
# WHEN TO RE-RUN THIS SCRIPT:
#   - Once, after merging the renderer change.
#   - If render-caddy-compose.sh changes in this repo and you want the
#     new version on the hosts immediately (otherwise the next CI deploy
#     of any service will eventually push it via scripts/ci/update-caddy.sh).
#   - If a Hetzner host is replaced/reimaged.
#   - If the @reboot crontab entry is removed.
#
# USAGE:
#
#   bash scripts/bootstrap-caddy-autostart.sh
#
# SAFETY:
#
# - No sudo. No root. Operates entirely within the deploy user's scope.
# - Idempotent: scp overwrites; crontab install checks for the marker
#   first and is a no-op if already present; compose render is a no-op
#   if the file is unchanged.
# - `set -e` aborts on first error so a failure on rishi-1 does not
#   silently leave rishi-2 half-configured. To recover from a partial
#   run, simply re-execute — every step is idempotent.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Hosts to configure. Pulled here (not from servers.config) because this
# is a one-shot operator script — adding a 4th server means adding it
# here AND running this once for the new host.
HOSTS=(
  "deploy@138.201.137.181"   # rishi-1
  "deploy@136.243.150.84"    # rishi-2
  "deploy@136.243.147.225"   # rishi-3
)

SSH_KEY="${SSH_KEY:-$HOME/.ssh/rishi-hetzner-ci-key}"
SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

LOCAL_RENDERER="${REPO_DIR}/caddy/render-caddy-compose.sh"

# Idempotency marker for the crontab entry. Greppable, stable across
# revisions of the actual command line. If we ever change CRON_LINE
# below, bump the marker too so old installs get replaced cleanly.
CRON_MARKER="# yral-chat-ai: restart caddy on boot (defends against docker stop + reboot)"

# The 30-second sleep gives docker.service time to settle before we
# `docker compose up -d`. Output is appended to /home/deploy/caddy-autostart.log
# which the deploy user can tail without sudo.
CRON_LINE="@reboot sleep 30 && cd /home/deploy/caddy && docker compose up -d >> /home/deploy/caddy-autostart.log 2>&1"

# Preflight on the operator's laptop.
if [[ ! -f "$LOCAL_RENDERER" ]]; then
  echo "ERROR: $LOCAL_RENDERER not found. Run from repo root." >&2
  exit 1
fi
if [[ ! -f "$SSH_KEY" ]]; then
  echo "ERROR: SSH key $SSH_KEY not found. Override with SSH_KEY=<path>." >&2
  exit 1
fi

echo "==> Bootstrapping Caddy auto-start on ${#HOSTS[@]} hosts"
echo "    renderer : $LOCAL_RENDERER"
echo "    method   : cron @reboot + restart: always (no sudo required)"
echo "    key      : $SSH_KEY"
echo ""

for host in "${HOSTS[@]}"; do
  echo "────────────────────────────────────────────────────────────────"
  echo "  Host: $host"
  echo "────────────────────────────────────────────────────────────────"

  # Step 1 — push the updated renderer to the host.
  echo "  [1/4] scp render-caddy-compose.sh → /home/deploy/caddy/"
  scp "${SSH_OPTS[@]}" "$LOCAL_RENDERER" "${host}:/home/deploy/caddy/render-caddy-compose.sh"
  ssh "${SSH_OPTS[@]}" "$host" "chmod +x /home/deploy/caddy/render-caddy-compose.sh"

  # Step 2 — re-render compose with the new restart policy and apply.
  # render-caddy-compose.sh is idempotent: if the compose file is
  # unchanged it skips the up; if it changed (which it WILL on the
  # first run of this bootstrap because of the unless-stopped→always
  # flip) it runs `docker compose up -d` to recreate Caddy.
  echo "  [2/4] re-render compose + apply (recreates Caddy with restart: always)"
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$host" "bash /home/deploy/caddy/render-caddy-compose.sh"

  # Step 3 — install the @reboot crontab line. Idempotent: greps for the
  # marker and is a no-op if found.
  echo "  [3/4] install @reboot crontab line (idempotent)"
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$host" bash -s <<REMOTE_BOOTSTRAP
    set -euo pipefail
    marker='${CRON_MARKER}'
    line='${CRON_LINE}'
    existing=\$(crontab -l 2>/dev/null || true)
    if printf '%s\n' "\${existing}" | grep -qxF "\${marker}"; then
      echo '    (marker already present; leaving crontab unchanged)'
    else
      echo '    (adding marker + @reboot line to crontab)'
      {
        printf '%s\n' "\${existing}"
        printf '%s\n' "\${marker}"
        printf '%s\n' "\${line}"
      } | crontab -
    fi
REMOTE_BOOTSTRAP

  # Step 4 — verify: print the crontab entry, the Caddy container's
  # restart policy, and that it's currently running. Operator should
  # see RestartPolicy=always on every host.
  echo "  [4/4] verify"
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$host" bash -s <<'REMOTE_VERIFY'
    echo '    --- crontab entry for yral-chat-ai ---'
    crontab -l 2>/dev/null | grep -B0 -A1 -F 'yral-chat-ai' || echo '    (NOT FOUND — bug in install step)'
    echo '    --- caddy restart policy + status ---'
    docker inspect caddy --format '    RestartPolicy={{.HostConfig.RestartPolicy.Name}}  State={{.State.Status}}  Health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}'
REMOTE_VERIFY

  echo ""
done

echo "================================================================"
echo "DONE. All ${#HOSTS[@]} hosts are now configured to start Caddy"
echo "automatically on boot, AND to keep it running after any manual"
echo "docker stop / docker daemon restart / SIGTERM."
echo ""
echo "Verify on a future boot: SSH in and run 'crontab -l'"
echo "Autostart logs: tail -f /home/deploy/caddy-autostart.log"
echo "================================================================"
