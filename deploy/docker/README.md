# 0xone-assistant — Docker deployment

Operational reference for the bot's Docker stack. Phase 5d retires
the systemd unit (kept as documented fallback in `deploy/systemd/`)
and ships the daemon as a versioned container image on GHCR plus a
two-service compose stack on the VPS.

## Architecture

| Component | Image | Purpose |
|-----------|-------|---------|
| `0xone-assistant` | `ghcr.io/c0manch3/0xone-assistant:<TAG>` | The bot daemon. Bind-mounts `~/.claude/` + data dir for state. |
| `autoheal` | `willfarrell/autoheal:latest` | Watchdog sidecar — restarts the bot container on `unhealthy` status (compose's `restart: unless-stopped` does NOT do this on its own). |

State paths inside the container (all bind-mounted from host):

| Container path | Host path | Purpose |
|----------------|-----------|---------|
| `/home/bot/.claude/` | `~/.claude/` | OAuth creds + projects + sessions + agents/skills/plugins. Full mount required (W2-C4). |
| `/home/bot/.local/share/0xone-assistant/` | `~/.local/share/0xone-assistant/` | Vault, sqlite DBs, `.daemon.pid`, `.last_clean_exit`, audit logs. |
| `/home/bot/.config/0xone-assistant/` | (read via `env_file`, no mount) | `.env` (TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID, WHISPER_API_URL) + `secrets.env` (WHISPER_API_TOKEN, optional GH_TOKEN). |

## Prerequisites

- Linux host with Docker CE >= 24 + compose plugin >= v2.20 (the
  `env_file` object syntax with `required: false` requires v2.20+).
- VPS user with uid:gid `1000:1000` owning `~/.claude/`,
  `~/.local/share/0xone-assistant/`, `~/.config/0xone-assistant/`.
- `TELEGRAM_BOT_TOKEN` + `OWNER_CHAT_ID` in
  `~/.config/0xone-assistant/.env`.
- Optional `GH_TOKEN` (fine-grained PAT, `read:packages` is enough
  for marketplace discovery) in `~/.config/0xone-assistant/secrets.env`.
- Outbound HTTPS to `api.anthropic.com`, `api.telegram.org`, and
  `ghcr.io`. No inbound ports needed (Telegram long polling).
- GHCR package visibility is **Private** (single-user bot; image
  contains personal-vault paths and skill manifests). VPS authenticates
  pulls via `docker login ghcr.io` with a fine-grained PAT scoped to
  `read:packages` only — see "First-time GHCR docker login" below.

## First-time GHCR docker login

GHCR creates new packages **PRIVATE** by default. We keep them private
(owner Q-R2 reversed 2026-04-26) — VPS authenticates pulls via PAT.

**One-shot setup on VPS (after first CI green):**

1. Owner creates a **classic** PAT (fine-grained PATs do NOT reliably
   support GHCR auth as of 2026 — GitHub's two PAT systems share the
   `packages` namespace but only classic propagates to ghcr.io login):
   - https://github.com/settings/tokens
   - Click **Generate new token (classic)** in the dropdown.
   - Note: `0xone-assistant-vps-pull`.
   - Expiration: 90 days or 1 year (re-run login after rotation).
   - Scopes: tick **only** `read:packages` under the
     `write:packages` group (Download packages from GitHub Package
     Registry). Everything else unchecked.
   - Copy the `ghp_*` token immediately — it's shown once.

2. SSH to VPS, log into GHCR (token via stdin — never on argv):
   ```bash
   ssh -i ~/.ssh/bot 0xone@193.233.87.118
   read -rs GHCR_PAT          # paste token, press Enter (silent input)
   echo "$GHCR_PAT" | docker login ghcr.io -u c0manch3 --password-stdin
   unset GHCR_PAT             # clear from shell history/env
   # Verify:
   docker pull ghcr.io/c0manch3/0xone-assistant:phase5d
   ```

   Credentials are stored in `~/.docker/config.json` (mode 0o600,
   default). All future `docker compose pull` reuse them — no re-login
   per deploy.

**Token rotation:** every PAT-expiry cycle, repeat step 2 with a new
token. `docker logout ghcr.io` first if you want to fully revoke
(daemon doesn't restart; new pulls just need fresh auth).

**Note on PAT type:** GitHub has two PAT systems. Classic PATs work
with `docker login ghcr.io`; fine-grained PATs have a `Packages`
permission in their UI but it doesn't propagate to GHCR auth as of
2026 (separate auth path under the hood). Stuck with classic PAT —
the `read:packages` scope is global across the user's packages but
that's fine for a single-user account that owns one container.

## Phase 6c first-time bootstrap (Mac sidecar + SSH tunnel + secrets)

> **Phase 6c hotfix — SSH reverse tunnel bootstrap.** Replaces the
> earlier Tailscale flow (Tailscale's default-route capture conflicts
> with AmneziaVPN on the Mac). Run these steps in order on a fresh
> deployment before `docker compose up -d`. The Mac sidecar must be
> reachable BEFORE the bot boots so the first `health_check()` lands.

1. **Run `setup-mac-sidecar.sh` on the Mac mini.** SSH or sit at the
   Mac:
   ```sh
   cd /path/to/0xone-assistant/whisper-server
   ./setup-mac-sidecar.sh
   ```
   The script generates the `WHISPER_API_TOKEN` (printed once) and a
   dedicated `~/.ssh/whisper_tunnel` ed25519 key (public key printed
   once with the exact `restrict,permitlisten="9000"` prefix to
   paste).

2. **Add the SSH public key to VPS `authorized_keys`.** From your
   laptop / the Mac, paste the line printed by the setup script:
   ```sh
   ssh 0xone@193.233.87.118 'cat >> ~/.ssh/authorized_keys' <<'EOF'
   restrict,permitlisten="9000",permitopen="" ssh-ed25519 AAAA…paste from Mac… whisper-tunnel-mac-mini
   EOF
   ```
   The `restrict,permitlisten="9000",permitopen=""` prefix is
   load-bearing — it locks the key to a single reverse-listener on
   port 9000, denying shell access, port-opens, agent forwarding,
   X11, etc. Without it the key would grant a regular login shell.

3. **Enable `GatewayPorts` on VPS sshd.** Without this, the reverse
   listener binds only to `127.0.0.1` inside the VPS network
   namespace and the docker bridge cannot reach it.
   ```sh
   ssh -i ~/.ssh/bot 0xone@193.233.87.118
   sudo sed -i 's/^#\?GatewayPorts.*/GatewayPorts yes/' /etc/ssh/sshd_config
   sudo sshd -t                          # syntax check before reload
   sudo systemctl reload sshd
   ```
   Verify: `grep '^GatewayPorts' /etc/ssh/sshd_config` → `GatewayPorts yes`.

4. **Drop the Whisper bearer token into VPS secrets.**
   ```sh
   ssh -i ~/.ssh/bot 0xone@193.233.87.118
   mkdir -p ~/.config/0xone-assistant
   cat > ~/.config/0xone-assistant/secrets.env <<'EOF'
   WHISPER_API_TOKEN=<paste from Mac setup>
   EOF
   chmod 600 ~/.config/0xone-assistant/secrets.env
   ```

5. **Set `WHISPER_API_URL` in the bot `.env`.** With the SSH-tunnel
   pivot the bot reaches the Mac via `host.docker.internal` (resolved
   by the docker bridge `extra_hosts: host-gateway` mapping):
   ```sh
   echo 'WHISPER_API_URL=http://host.docker.internal:9000' \
     >> ~/.config/0xone-assistant/.env
   ```

6. **Tunnel sanity check from VPS.** After the Mac boots and the
   `com.zeroxone.whisper-tunnel` LaunchAgent is up, the VPS should
   show a listener on `0.0.0.0:9000`:
   ```sh
   ssh -i ~/.ssh/bot 0xone@193.233.87.118 \
     "ss -ltn | grep ':9000'"
   # → tcp LISTEN 0 128 0.0.0.0:9000  0.0.0.0:*

   ssh -i ~/.ssh/bot 0xone@193.233.87.118 \
     "curl -s http://172.17.0.1:9000/health"
   # → {"status":"ok","model_loaded":true,...}
   ```
   If you see `127.0.0.1:9000` instead of `0.0.0.0:9000`,
   `GatewayPorts yes` is not active — re-check step 3.

7. **`docker compose up -d`.**
   ```sh
   cd /opt/0xone-assistant/deploy/docker
   docker compose pull
   docker compose up -d
   docker compose ps          # bot healthy after ~30-60s
   ```

8. **Owner Telegram smoke.** Record a 10-second voice → bot
   transcribes + Claude responds (AC#1). Send a 30-min YouTube URL
   prefixed with `транскрибируй ` → bot acks + extracts + summarises
   (AC#4). If the smoke fails on AC#5 (Mac sidecar offline), check
   the tunnel state on the Mac:
   `launchctl list | grep whisper-tunnel` (last column is exit code;
   non-zero = the tunnel just crashed) and the autossh log under
   `~/whisper-server/logs/whisper-tunnel.err`.

## Initial install (fresh VPS)

Follow this sequence in order. Steps 1-2 are read-only; steps 3+
modify host state.

```bash
# === On Mac (commit + push triggers CI; image must be pushed first) ===
git push origin main
# Wait for CI green at https://github.com/c0manch3/0xone-assistant/actions
# Then: GHCR visibility flip (see section above).

# === SSH to VPS ===
ssh -i ~/.ssh/bot 0xone@193.233.87.118

# Step 1: verify pull works BEFORE touching VPS state.
docker --version && docker compose version
docker pull ghcr.io/c0manch3/0xone-assistant:phase5d
# If pull fails with 401: GHCR visibility not flipped yet. Stop, fix, retry.
# If `docker` is missing: install via `curl -fsSL https://get.docker.com | sh`
# then `sudo usermod -aG docker 0xone` and re-SSH.

# Step 2: backup state (read-only; can't fail badly).
BACKUP=~/.backup-$(date +%Y%m%d-%H%M)
mkdir -p $BACKUP
cp -a ~/.claude $BACKUP/
cp -a ~/.local/share/0xone-assistant $BACKUP/
cp -a ~/.config/0xone-assistant $BACKUP/
echo "Backup at $BACKUP — total $(du -sh $BACKUP | awk '{print $1}')"

# Step 2.5: fix ~/.claude subdir ownership (one-off, idempotent).
# Spike RQ7 found root-owned subdirs from prior `sudo claude ...` runs.
test -d ~/.claude && \
  test "$(stat -c %U ~/.claude)" = "$USER" && \
  sudo chown -R 0xone:0xone ~/.claude || true

# Step 3: stop systemd FIRST (BLOCKING — skipping causes container
# restart hot-loop from singleton-lock BlockingIOError, RQ12).
systemctl --user disable --now 0xone-assistant.service || true
systemctl --user is-active 0xone-assistant.service && exit 1 || true

# Step 4: bring repo up-to-date for new compose artifacts.
cd /opt/0xone-assistant && git pull

# Step 5: first boot.
cd /opt/0xone-assistant/deploy/docker
echo "TAG=phase5d" > .env       # or: cp .env.example .env && edit TAG
docker compose pull
docker compose up -d
docker compose ps               # both services Up; bot healthy after ~60s
docker compose logs --tail=200

# Step 6: owner Telegram smoke (manual).
# /ping, memory_save, memory_find, scheduler_add, marketplace_list.

# Step 7: reboot test.
sudo reboot
# After ~60s SSH back:
docker compose ps               # both services Up (healthy)

# Step 8: 24h after green prod, retire systemd unit.
rm ~/.config/systemd/user/0xone-assistant.service
systemctl --user daemon-reload
# Repo copy in deploy/systemd/ retained as documented fallback.
```

## Update to a new image

```bash
cd /opt/0xone-assistant && git pull       # pulls compose changes if any
cd deploy/docker
echo "TAG=sha-NEW_HASH" > .env            # or TAG=phase5e
docker compose pull
docker compose up -d                      # compose detects TAG change + recreates
docker compose logs -f --tail=200
# Telegram smoke.
```

## Rollback

```bash
cd /opt/0xone-assistant/deploy/docker

# Pre-flight: confirm the rollback tag still exists in GHCR.
# (sha-* tags older than 30d may be auto-pruned in phase 9.)
docker manifest inspect ghcr.io/c0manch3/0xone-assistant:$TAG

echo "TAG=sha-OLD_HASH" > .env             # or TAG=phase5d
docker compose pull
docker compose up -d --force-recreate
```

If the rollback tag was pruned, fall back to the nearest `:phaseN`
tag (those are never auto-pruned).

**Absolute fallback:** the systemd unit at
`~/.config/systemd/user/0xone-assistant.service` (kept disabled).
`docker compose stop && systemctl --user enable --now 0xone-assistant`
restores the phase 5a state.

## Backup

Recommended cadence: before every update + weekly cron.

```bash
BACKUP=~/.backup-$(date +%Y%m%d-%H%M)
mkdir -p $BACKUP
cp -a ~/.claude $BACKUP/
cp -a ~/.local/share/0xone-assistant $BACKUP/
cp -a ~/.config/0xone-assistant $BACKUP/
echo "Backup at $BACKUP — total $(du -sh $BACKUP | awk '{print $1}')"
```

For sqlite specifically, use `.backup` (online-safe, no torn pages):

```bash
DB=~/.local/share/0xone-assistant/assistant.db
sqlite3 "$DB" ".backup '$BACKUP/assistant-$(date +%F).db'"
```

## OAuth + GH_TOKEN handling

### Claude OAuth (`~/.claude/`)

Bind-mounted rw. The bundled claude binary refreshes `access_token`
every ~1h via `os.rename(tmp, target)` — atomic on Linux/ext4 bind
mounts (RQ4 verified).

**RULE:** never run `sudo claude` on the VPS. Files inside
`~/.claude/` created with root ownership block container writes
(uid 1000). If debugging on VPS, `su - 0xone` first.

If you accidentally run as root:

```bash
sudo chown -R 0xone:0xone ~/.claude
```

### GH_TOKEN rotation

Compose detects env_file content changes via config hash and
recreates the container automatically:

```bash
vi ~/.config/0xone-assistant/secrets.env       # set GH_TOKEN=...
cd /opt/0xone-assistant/deploy/docker
docker compose up -d                           # recreates with new env
docker compose logs -f --tail=50
```

**Do NOT use:**
- `docker compose restart` — reuses stale env (env baked at create time).
- `docker exec ... kill -HUP 1` — pid 1's env is frozen at exec time;
  the daemon has no SIGHUP handler for env reload anyway.

## Healthcheck

```bash
docker compose ps                                              # status column
docker inspect --format '{{.State.Health.Status}}' 0xone-assistant
docker inspect --format '{{json .State.Health}}' 0xone-assistant | jq
```

The healthcheck verifies that `/home/bot/.local/share/0xone-assistant/.daemon.pid`
exists, has a non-empty pid, AND that pid resolves via
`/proc/$pid/exe` to a python interpreter. The exe-readlink step
eliminates pid-recycle false positives AND empty-pid-file races
(W2-C3, W2-M5).

`start_period: 60s` covers worst-case claude preflight (45s timeout +
slack — RQ12). If the container repeatedly enters `unhealthy` past
the start period, the autoheal sidecar restarts it (max once every
30s). Repeated cycles indicate a real failure — read logs.

To force-bump if owner observes flapping:

```yaml
# in docker-compose.yml under healthcheck:
start_period: 90s
```

## Log tailing

```bash
docker compose logs -f 0xone-assistant
docker compose logs -f --tail=200 0xone-assistant
docker compose logs --since 1h 0xone-assistant | jq -R 'fromjson?'
```

`docker compose logs` UN-wraps the json-file driver's outer envelope,
so each line is the daemon's structlog JSON event directly. If you
read raw container logs at
`/var/lib/docker/containers/<id>/<id>-json.log`, each line is double-
encoded (`{"log":"...inner json...\n","stream":"stdout","time":"..."}`)
— use `jq -r .log | jq .` to unwrap.

## `.daemon.pid` is container-namespaced

`.daemon.pid` contains the **container-namespace** pid (typically
`7` because tini is pid 1 and the python child gets pid 7 after
exec). Reading it from the host and running `kill -0 <pid>` on the
host is meaningless — that pid in the host namespace is unrelated.

For host-side debugging:

```bash
docker compose ps                                # service state
docker compose logs                              # daemon events
docker compose top                               # processes inside container
docker exec 0xone-assistant ps -ef               # full process tree
docker inspect --format '{{.State.Pid}}' 0xone-assistant   # host pid of pid 1
```

## Transcript pruning

`~/.claude/projects/` grows unbounded — one JSONL file per Claude
Code session. Recommended: prune monthly or when disk pressure
appears.

```bash
# Inspect first.
du -sh ~/.claude/projects/

# Delete sessions older than 60 days.
find ~/.claude/projects/ -name '*.jsonl' -mtime +60 -delete
```

Phase 9 hardening will tighten container access to `~/.claude/`
beyond the current rw bind mount.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `compose pull` returns `unauthorized: authentication required` | GHCR PAT expired or never installed. SSH VPS, run the one-shot login from "First-time GHCR docker login" section above with a fresh fine-grained PAT scoped `read:packages`. |
| Container stuck `starting` past 90s | Check `docker compose logs` for `auth_preflight_ok` event. Missing -> OAuth bind-mount wrong or token expired. |
| `BlockingIOError` log spam right after `compose up` | systemd unit still active. `systemctl --user disable --now 0xone-assistant.service`, then `docker compose up -d`. |
| `Permission denied` writing inside `~/.claude/` | Some subdir owned by root from prior `sudo claude`. `sudo chown -R 0xone:0xone ~/.claude`. |
| `secrets.env: no such file or directory` on `compose up` | Compose < v2.20 (object form `required: false` not supported). Upgrade compose plugin: `sudo apt-get install --only-upgrade docker-compose-plugin`. |
| `compose up` errors `TAG must be set` | Set `TAG=phase5d` (or similar) in `deploy/docker/.env`. Never use `latest` in prod. |
| Container keeps restarting in a tight loop | Check `docker compose logs` for the pre-crash structured event. autoheal restarts on `unhealthy`; if the container exits non-zero in a loop, the root cause is in code/config, not infrastructure. |
| Healthcheck reports `healthy` but Telegram doesn't respond | `.daemon.pid` is written before all init completes (during the 30-45s claude preflight). The exe-readlink check passes because the process IS python, but the daemon isn't yet polling for updates. `start_period: 60s` covers this; if the owner sees `healthy` immediately after `compose up -d` and Telegram still silent, wait a full minute before assuming a real bug. Tail `docker compose logs` for `auth_preflight_ok` to confirm init finished. |
| `docker pull` rate-limited (anonymous) | GHCR allows 60 anon pulls/h per IP. Authenticate the pull: `echo $GH_TOKEN | docker login ghcr.io -u c0manch3 --password-stdin`. |

## Migration from systemd (one-time, VPS)

See full 11-step playbook in `plan/phase5d/description.md §F`. TL;DR:

1. CI image pushed; VPS `docker login ghcr.io` with read:packages PAT (one-shot).
2. `docker pull ghcr.io/c0manch3/0xone-assistant:<tag>` works on VPS (test before destructive steps).
3. (Only if docker missing) install docker.
4. Backup state to `~/.backup-<timestamp>`.
5. `chown -R 0xone:0xone ~/.claude` (one-off, safe-wrapped).
6. `systemctl --user disable --now 0xone-assistant.service`.
7. `git pull`, set TAG, `docker compose pull && up -d`.
8. Owner Telegram smoke.
9. `sudo reboot`, verify both services Up healthy.
10. 24h-green gate, then retire systemd unit file.

Failure rollback at any step:
`docker compose stop && systemctl --user enable --now 0xone-assistant.service`.

## Host-level: Docker daemon shutdown timeout

Compose's `stop_grace_period: 35s` only applies to `docker stop` /
`docker compose down`. On host reboot, the Docker daemon's own
`shutdown-timeout` (default 10s) takes over — too short for the
35s daemon shutdown path. Set in `/etc/docker/daemon.json`:

```json
{
  "shutdown-timeout": 40
}
```

Then `sudo systemctl restart docker`.

## Phase 9 hardening (deferred)

Tracked in `plan/phase5d/description.md §M`:

- `read_only: true` for container fs (write paths via tmpfs / bind).
- `cap_drop: [ALL]` + `no-new-privileges: true`.
- `tmpfs: ["/tmp"]`.
- Rootless Docker on VPS.
- Trivy SARIF upload to GitHub Security tab.
- Cosign image signing + SBOM.
- Digest-pinned GHA actions (currently major-version pinned).
- arm64 image (PyStemmer arm64 wheel availability or native runner).
- GHCR retention policy automation.
