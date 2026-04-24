# systemd unit — `0xone-assistant.service`

User-scope systemd unit that runs the bot daemon on a Linux host (VPS
or workstation). Installed to `~/.config/systemd/user/` so it survives
package upgrades and never needs root.

## Prereqs

- Non-root user on the target host (example below uses `0xone`).
- `uv` installed at `~/.local/bin/uv` (or adjust `ExecStart`).
- Repo cloned to `/opt/0xone-assistant` (or adjust `WorkingDirectory`).
- `~/.config/0xone-assistant/.env` populated with
  `TELEGRAM_BOT_TOKEN` + `OWNER_CHAT_ID` (phase 1 invariant).
- `claude` CLI installed + logged in as the same user (OAuth session
  lives under `~/.claude/`).
- `gh` CLI installed + authenticated (for the installer's
  `marketplace_list` tool). If `gh auth login` via SSH is awkward,
  mirror the auth from a dev host via:
  ```bash
  # on dev host where gh is already logged in:
  gh auth token | ssh <user>@<vps> \
    "mkdir -p ~/.config/0xone-assistant && \
     echo \"GH_TOKEN=\$(cat)\" > ~/.config/0xone-assistant/secrets.env && \
     chmod 600 ~/.config/0xone-assistant/secrets.env"
  ```
  The unit picks up `GH_TOKEN` via `EnvironmentFile=-%h/.config/0xone-assistant/secrets.env`
  (leading `-` makes it optional — missing file is fine for local dev
  where `gh` is already logged in at the shell level).

## Install

```bash
mkdir -p ~/.config/systemd/user
cp /opt/0xone-assistant/deploy/systemd/0xone-assistant.service \
   ~/.config/systemd/user/0xone-assistant.service
systemctl --user daemon-reload
systemctl --user enable --now 0xone-assistant
journalctl --user -u 0xone-assistant -f
```

To let the bot run without an active login session (e.g. survive
`logout` / reconnect):

```bash
loginctl enable-linger "$USER"
```

## Update (after `git pull`)

```bash
cd /opt/0xone-assistant && git pull && uv sync
systemctl --user restart 0xone-assistant
```

If the unit itself changed, also:

```bash
cp /opt/0xone-assistant/deploy/systemd/0xone-assistant.service \
   ~/.config/systemd/user/0xone-assistant.service
systemctl --user daemon-reload
systemctl --user restart 0xone-assistant
```

## Verify

```bash
systemctl --user status 0xone-assistant
systemctl --user show 0xone-assistant | grep -E 'TimeoutStop|Restart'
```

`TimeoutStopSec` should read `30s` (Fix 14). `Restart=on-failure` +
`RestartSec=10s` are the rolling-backoff settings.

## Troubleshoot

- Service stuck restarting → `journalctl --user -u 0xone-assistant
  --since '5 min ago'` and look for the structured event preceding
  the crash. `bg_task_giving_up` means the supervisor gave up on a
  scheduler subtask; see `plan/phase5/runbook.md §7`.
- Clean stop not classified clean → verify
  `~/.local/share/0xone-assistant/.last_clean_exit` mtime at
  `TimeoutStopSec` expiry; if the file is missing, the write was
  pre-empted. Bump `TimeoutStopSec` to 60s if an in-flight Claude
  call routinely exceeds 30s.

## Optional hardening (phase 9+)

```
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/0xone/.local/share/0xone-assistant /home/0xone/.config/0xone-assistant /home/0xone/.claude /opt/0xone-assistant
ProtectHome=false
```

Deliberately not in the default unit yet — the `ReadWritePaths` list
is user-specific and we prefer a simple bootable default.
