# Whisper sidecar — Mac mini Apple Silicon companion service

Phase 6c sidecar for the 0xone-assistant Telegram bot. Runs locally on
the owner's Mac mini and exposes Whisper transcription over Tailscale
to the VPS-hosted bot. NOT in Docker — `mlx-whisper` requires direct
Metal/MLX access to the Apple Silicon GPU.

## Architecture

```
Telegram → bot (VPS, Docker)  ── Tailscale ──>  Mac mini whisper-server
                                                  ├─ FastAPI :9000
                                                  ├─ ffmpeg
                                                  ├─ yt-dlp
                                                  └─ mlx-whisper (Whisper Large v3 Turbo)
```

Two endpoints + a health probe:

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /transcribe` | Bearer | Multipart audio upload → transcribe |
| `POST /extract` | Bearer | JSON `{url}` body → yt-dlp + transcribe |
| `GET /health` | None | `{status, model_loaded, yt_dlp_version}` |

## One-time setup

### Mac mini (this machine)

Pre-flight:
- Apple Silicon (`uname -m == arm64`)
- macOS 13.5+ (Sequoia 15.x recommended)
- Always-logged-in account (sidecar runs as user; LaunchAgent dies on
  logout — auto-login is the canonical owner setup)

Run from a fresh checkout of the bot repo:

```sh
cd /path/to/0xone-assistant/whisper-server
./setup-mac-sidecar.sh
```

The script:

1. Installs Homebrew (if missing), `ffmpeg`, `python@3.12`, and the
   Tailscale CLI cask.
2. Creates `~/whisper-server/.venv` (Python 3.12) and installs
   `requirements.txt`.
3. Generates a 32-char URL-safe bearer token in
   `~/.config/whisper-server/.env` (mode 600). **Copies the token to
   stdout once — paste it into the VPS `~/.config/0xone-assistant/secrets.env`
   as `WHISPER_API_TOKEN=…`.**
4. Runs `tailscale up --advertise-tags=tag:whisper-mac` (interactive
   browser auth on first run). Prints the MagicDNS hostname for VPS
   `WHISPER_API_URL`.
5. Installs two LaunchAgents in `~/Library/LaunchAgents/`:
   - `com.zeroxone.whisper-server.plist` — autostart + KeepAlive
   - `com.zeroxone.yt-dlp-update.plist` — daily 04:00 `pip install -U yt-dlp`
6. Pre-downloads the Whisper model (~1.6 GB) into the HuggingFace
   cache so the first real request is fast.

### Tailscale ACL (admin console)

Apply this snippet to your tailnet's policy file. Default-deny so
only the bot VPS can reach the Mac on port 9000 (defence-in-depth on
top of the bearer token):

```hujson
{
  "tagOwners": {
    "tag:bot-vps":     ["autogroup:admin"],
    "tag:whisper-mac": ["autogroup:admin"],
  },
  "acls": [
    {
      "action": "accept",
      "src":    ["tag:bot-vps"],
      "dst":    ["tag:whisper-mac:9000"],
    },
    {
      "action": "accept",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:bot-vps:22", "tag:whisper-mac:22"],
    },
  ],
  "ssh": [
    {
      "action": "check",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:bot-vps", "tag:whisper-mac"],
      "users":  ["autogroup:nonroot", "root"],
    },
  ],
}
```

### VPS (bot) side

> **F5 (fix-pack) — Tailscale auth key requirements.**
>
> `TS_AUTHKEY` MUST be a **reusable, non-ephemeral, preauthorized**
> auth key tagged `tag:bot-vps`. Generate at
> <https://login.tailscale.com/admin/settings/keys> with these
> checkboxes enabled:
> - **Reusable** ✅ (so the bot can reauthenticate after
>   `tailscale-state` volume loss)
> - **Preauthorized** ✅ (no manual approval after `tailscale up`)
> - **Ephemeral** ❌ (we want the node to persist across container
>   restarts; ephemeral nodes are deleted ~5 min after disconnect)
> - **Tags:** `tag:bot-vps`
>
> Single-use keys WILL break the bot the first time the
> `tailscale-state` volume is rebuilt (compose-down + up, host
> rebuild, anything that recreates the named volume).

> **F4 (fix-pack) — secrets file split.**
>
> The Tailscale auth key and the Whisper API token live in **separate
> files** so `docker inspect` on either container reveals only that
> service's secrets:
> - `~/.config/0xone-assistant/secrets.env` — `WHISPER_API_TOKEN=…`
>   and any `GH_TOKEN=…`. Loaded by the bot service only.
> - `~/.config/0xone-assistant/secrets-tailscale.env` — `TS_AUTHKEY=…`.
>   Loaded by the tailscale sidecar only.
>
> Both files mode 600. Both NOT in git.

Create the two secrets files:

```sh
# Bot secrets — Whisper bearer token from the Mac setup output.
cat > ~/.config/0xone-assistant/secrets.env <<'EOF'
WHISPER_API_TOKEN=<paste from Mac setup>
EOF
chmod 600 ~/.config/0xone-assistant/secrets.env

# Tailscale secrets — auth key from the admin console.
cat > ~/.config/0xone-assistant/secrets-tailscale.env <<'EOF'
TS_AUTHKEY=tskey-auth-...
EOF
chmod 600 ~/.config/0xone-assistant/secrets-tailscale.env
```

In `~/.config/0xone-assistant/.env` set:

```
WHISPER_API_URL=http://<mac-mini-magicdns-name>:9000
```

Bring up the Tailscale sidecar + bot:

```sh
docker compose -f deploy/docker/docker-compose.yml up -d
```

## Operations

### Smoke test

From the VPS:
```sh
docker exec 0xone-assistant curl -s \
  -H "Authorization: Bearer $WHISPER_API_TOKEN" \
  "http://<mac-mini>:9000/health"
# → {"status":"ok","model_loaded":true,"yt_dlp_version":"2026.04.15"}
```

From the Mac (sanity):
```sh
curl http://localhost:9000/health
# Should NOT carry the bearer token; a missing-bearer response on
# /transcribe means the auth check is wired correctly.
```

### Logs

```
~/whisper-server/logs/whisper-server.log
~/whisper-server/logs/whisper-server.err
~/whisper-server/logs/yt-dlp-update.log
~/whisper-server/logs/yt-dlp-update.err
```

### Restart after config changes

```sh
launchctl kickstart -k "gui/$(id -u)/com.zeroxone.whisper-server"
```

### Token rotation

1. Edit `~/.config/whisper-server/.env` on the Mac with a fresh token
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. Mirror the new token into VPS `~/.config/0xone-assistant/secrets.env`.
3. Restart on both sides:
   - Mac: `launchctl kickstart -k "gui/$(id -u)/com.zeroxone.whisper-server"`
   - VPS: `docker compose restart 0xone-assistant`

### Troubleshooting

| Symptom | Diagnosis |
|---|---|
| `/health` returns 200 but `model_loaded: false` | Prewarm failed (network blip during HuggingFace download). `launchctl kickstart -k …` to retry. |
| `401 invalid bearer` from bot logs | Token mismatch. Compare `cat ~/.config/whisper-server/.env` (Mac) with `cat ~/.config/0xone-assistant/secrets.env` (VPS). |
| `whisper_extract_connect_error` in bot logs | Mac asleep or Tailscale down. `tailscale status` on Mac. |
| `yt-dlp` returns 422 with "confirm you are not a bot" | YouTube anti-bot caught us. Owner can manually run `yt-dlp -U` and retry; if persistent, fall back to direct file upload. |
| `507 Insufficient Storage` | Mac disk free <2 GB. `df -h ~/whisper-server`. |

## Caveats

- **Mac asleep**: the bot replies "Mac sidecar offline" and the voice
  is dropped (NO queue, NO retry). Plan for the Mac to be awake when
  recording voice notes; configure `pmset` if needed.
- **Always-logged-in**: LaunchAgents (not LaunchDaemons) die on logout.
  We use a LaunchAgent because mlx requires the GPU which is bound to
  a logged-in user session.
- **Cookie-less yt-dlp**: works for podcasts / Spotify / SoundCloud /
  Vimeo / most lectures. For owner-restricted YouTube videos, manually
  add `--cookies-from-browser firefox` to `yt-dlp` invocation in
  `main.py`. NOT included by default to avoid the privacy concern of
  yt-dlp reading the entire cookie store.
