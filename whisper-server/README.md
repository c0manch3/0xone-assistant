# Whisper sidecar — Mac mini Apple Silicon companion service

Phase 6c sidecar for the 0xone-assistant Telegram bot. Runs locally on
the owner's Mac mini and exposes Whisper transcription to the
VPS-hosted bot via an SSH reverse tunnel. NOT in Docker —
`mlx-whisper` requires direct Metal/MLX access to the Apple Silicon
GPU.

## Architecture

```
Telegram → bot (VPS, Docker)  ─────────────  Mac mini whisper-server
        │                                     ├─ FastAPI :9000 (loopback)
        │                                     ├─ ffmpeg
        │                                     ├─ yt-dlp
        │                                     └─ mlx-whisper (Whisper Large v3 Turbo)
        │                                            │
        │ http://host.docker.internal:9000           │
        ▼                                            │
   docker bridge 172.17.0.1:9000                     │
        ▲                                            │
        │ VPS sshd GatewayPorts yes                  │
        │                                            │
        └────────  SSH reverse tunnel  ◄─── autossh -N -R 9000:localhost:9000
                   (port 22, normal egress)
```

The Mac mini opens a long-lived SSH session to the VPS with reverse
port-forward `-R 9000:localhost:9000`. VPS sshd's `GatewayPorts yes`
re-publishes that listener on every interface, including the docker
bridge (`172.17.0.1`). The bot container reaches the Mac via
`host.docker.internal:9000` (compose `extra_hosts: host-gateway`).

This replaces the previous Tailscale design — Tailscale's default-
route capture conflicts with the owner's AmneziaVPN on the Mac.
SSH egress on port 22 goes through normal routing and works regardless
of AmneziaVPN state.

Two endpoints + a health probe:

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /transcribe` | Bearer | Multipart audio upload → transcribe |
| `POST /extract` | Bearer | JSON `{url}` body → yt-dlp + transcribe |
| `GET /health` | None | `{status, model_loaded, yt_dlp_version}` |

The bearer token is defence-in-depth on top of SSH-key transport auth
— both must check out for a request to land.

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

1. Installs Homebrew (if missing), `ffmpeg`, `python@3.12`, and
   `autossh` (keeps the reverse tunnel alive across NAT timeouts).
2. Creates `~/whisper-server/.venv` (Python 3.12) and installs
   `requirements.txt`.
3. Generates a 32-char URL-safe bearer token in
   `~/.config/whisper-server/.env` (mode 600). **Copies the token to
   stdout once — paste it into the VPS `~/.config/0xone-assistant/secrets.env`
   as `WHISPER_API_TOKEN=…`.**
4. Generates a dedicated SSH key at `~/.ssh/whisper_tunnel` (ed25519,
   no passphrase). **Prints the public key once** with the exact
   `restrict,permitlisten="9000",permitopen=""` prefix to paste into
   VPS `~/.ssh/authorized_keys`.
5. Installs three LaunchAgents in `~/Library/LaunchAgents/`:
   - `com.zeroxone.whisper-server.plist` — autostart + KeepAlive
   - `com.zeroxone.whisper-tunnel.plist` — autossh reverse tunnel
   - `com.zeroxone.yt-dlp-update.plist` — daily 04:00 `pip install -U yt-dlp`
6. Pre-downloads the Whisper model (~1.6 GB) into the HuggingFace
   cache so the first real request is fast.

### VPS (bot) side — SSH reverse tunnel bootstrap

After running `setup-mac-sidecar.sh` on the Mac, the script prints a
`ssh-ed25519 AAAA…` public key. Add it to VPS `authorized_keys` with
the port-forward restriction so the key cannot do anything else (no
shell, no other forwards, no command execution):

```sh
ssh 0xone@193.233.87.118 'cat >> ~/.ssh/authorized_keys' <<'EOF'
restrict,permitlisten="9000",permitopen="" ssh-ed25519 AAAA…paste from Mac… whisper-tunnel-mac-mini
EOF
```

`restrict` is OpenSSH shorthand for `no-agent-forwarding,no-port-forwarding,
no-pty,no-user-rc,no-x11-forwarding`. Combined with the explicit
`permitlisten="9000"` it allows ONLY a reverse-listener on port 9000
and nothing else.

Then enable `GatewayPorts` so sshd binds the reverse listener on all
interfaces (specifically the docker bridge so the bot container can
reach it via `host.docker.internal`):

```sh
ssh 0xone@193.233.87.118
sudo sed -i 's/^#\?GatewayPorts.*/GatewayPorts yes/' /etc/ssh/sshd_config
sudo sshd -t                          # config syntax check
sudo systemctl reload sshd
```

Drop the `WHISPER_API_TOKEN` printed by the Mac setup into the VPS
secrets file:

```sh
mkdir -p ~/.config/0xone-assistant
cat > ~/.config/0xone-assistant/secrets.env <<'EOF'
WHISPER_API_TOKEN=<paste from Mac setup>
EOF
chmod 600 ~/.config/0xone-assistant/secrets.env
```

In `~/.config/0xone-assistant/.env` set:

```
WHISPER_API_URL=http://host.docker.internal:9000
```

Bring up the bot:

```sh
docker compose -f deploy/docker/docker-compose.yml up -d
```

## Operations

### Smoke test

From the VPS host (sanity check that the tunnel binds 172.17.0.1):
```sh
ss -ltn | grep ':9000'
# tcp LISTEN 0 128 0.0.0.0:9000  0.0.0.0:*
curl -s http://172.17.0.1:9000/health
# → {"status":"ok","model_loaded":true,"yt_dlp_version":"2026.04.15"}
```

From inside the bot container:
```sh
docker exec 0xone-assistant curl -s \
  -H "Authorization: Bearer $WHISPER_API_TOKEN" \
  "http://host.docker.internal:9000/health"
```

From the Mac (sanity):
```sh
curl http://127.0.0.1:9000/health
# Should NOT carry the bearer token; a missing-bearer response on
# /transcribe means the auth check is wired correctly.
```

### Logs

```
~/whisper-server/logs/whisper-server.log
~/whisper-server/logs/whisper-server.err
~/whisper-server/logs/whisper-tunnel.log
~/whisper-server/logs/whisper-tunnel.err
~/whisper-server/logs/yt-dlp-update.log
~/whisper-server/logs/yt-dlp-update.err
```

### Restart after config changes

```sh
launchctl kickstart -k "gui/$(id -u)/com.zeroxone.whisper-server"
launchctl kickstart -k "gui/$(id -u)/com.zeroxone.whisper-tunnel"
```

### Token rotation

1. Edit `~/.config/whisper-server/.env` on the Mac with a fresh token
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. Mirror the new token into VPS `~/.config/0xone-assistant/secrets.env`.
3. Restart on both sides:
   - Mac: `launchctl kickstart -k "gui/$(id -u)/com.zeroxone.whisper-server"`
   - VPS: `docker compose restart 0xone-assistant`

### SSH key rotation

1. On Mac: `ssh-keygen -t ed25519 -f ~/.ssh/whisper_tunnel -N "" -C "whisper-tunnel-mac-mini"`
   (the existing file is overwritten — answer "y" at the prompt).
2. Copy the new public key into VPS `~/.ssh/authorized_keys`,
   replacing the old `whisper-tunnel-mac-mini` line. Keep the
   `restrict,permitlisten="9000",permitopen=""` prefix.
3. Restart the tunnel:
   `launchctl kickstart -k "gui/$(id -u)/com.zeroxone.whisper-tunnel"`.

### Troubleshooting

| Symptom | Diagnosis |
|---|---|
| `/health` returns 200 but `model_loaded: false` | Prewarm failed (network blip during HuggingFace download). `launchctl kickstart -k …` to retry. |
| `401 invalid bearer` from bot logs | Token mismatch. Compare `cat ~/.config/whisper-server/.env` (Mac) with `cat ~/.config/0xone-assistant/secrets.env` (VPS). |
| `whisper_extract_connect_error` in bot logs | Tunnel down. `launchctl list \| grep whisper-tunnel` on Mac (last column = exit code; non-zero = recent crash). On VPS: `ss -ltn \| grep ':9000'` should show `0.0.0.0:9000` LISTEN. If only `127.0.0.1:9000`, GatewayPorts is not enabled. |
| Tunnel never connects | Check VPS sshd accepts the key: `journalctl -u ssh -n 50` on VPS. Common: `authorized_keys` mode wrong (must be 600); user dir mode wrong (must be 700); `restrict` typo. |
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
- **AmneziaVPN compatibility**: the SSH tunnel uses normal egress on
  port 22, which is unaffected by AmneziaVPN's traffic shaping. This
  is the whole reason we pivoted away from Tailscale — Tailscale's
  default-route capture (its `tun` interface) conflicts with
  AmneziaVPN's own routing rules and breaks both connections.
