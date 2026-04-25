# Phase 4 memory subsystem runbook

Operational reference for the long-term memory MCP server shipped in
phase 4. Read this first when something looks wrong; update when the
on-disk layout or env surface changes.

## 1. Data directory layout

Default `data_dir = $XDG_DATA_HOME/0xone-assistant` (falls back to
`~/.local/share/0xone-assistant`). The memory subsystem creates these
artifacts on first boot:

```
<data_dir>/
  vault/                      # flat-file Markdown notes (Obsidian-compatible)
    inbox/
    projects/
    people/
    .tmp/                     # atomic-write staging (safe to delete at rest)
  memory-index.db             # FTS5 index (derived from vault; safe to rm)
  memory-index.db-wal         # sqlite WAL side-car
  memory-index.db-shm         # sqlite shared-memory side-car
  memory-index.db.lock        # fcntl advisory lock file (empty)
  memory-audit.log            # JSONL record of every mcp__memory__* call
  .daemon.pid                 # singleton lock (flock'd, pid inside)
```

All artifacts are `0o600` (owner-only) because note bodies contain
private data.

## 2. Env var reference

All memory knobs are optional. Put overrides in
`$XDG_CONFIG_HOME/0xone-assistant/.env` (falls back to
`~/.config/0xone-assistant/.env`) or the repo-local `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMORY_VAULT_DIR` | `<data_dir>/vault` | Point at an existing Obsidian vault. Must be on local APFS/ext4 — NOT iCloud/Dropbox/SMB. |
| `MEMORY_INDEX_DB_PATH` | `<data_dir>/memory-index.db` | Move the FTS5 index off the default disk. Keep on the same filesystem as the vault for atomic rename. |
| `MEMORY_MAX_BODY_BYTES` | `1048576` (1 MiB) | Per-note body cap. Reject-on-write; pre-existing larger notes are not retroactively truncated. |
| `MEMORY_ALLOW_LARGE_REINDEX` | unset | Opt-out of the 2000-note auto-reindex cap. Set to any non-empty value only if you know your boot-time reindex cost. |

## 3. Backup

The vault is flat Markdown — trivially rsyncable, Time-Machine-safe,
and git-friendly.

```bash
# Daily snapshot to ~/Backups
rsync -a --delete \
  ~/.local/share/0xone-assistant/vault/ \
  ~/Backups/vault-$(date +%F)/
```

Do NOT back up `memory-index.db*` — the index is fully derivable from
the vault. Backing it up wastes space and the restored copy can be
stale relative to the vault anyway.

Also exclude `vault/.tmp/` from any long-term backup; it holds
in-flight atomic-write staging files that a SIGKILL of the daemon
may leave behind.

## 4. Recovery: corrupt or missing index

Index is authoritative? No — **vault is authoritative**. If the index
drifts, simply delete it and restart:

```bash
rm ~/.local/share/0xone-assistant/memory-index.db*
# Restart the daemon. Boot-time auto-reindex rebuilds from the vault.
```

Auto-reindex is capped at 2000 notes by default; bump
`MEMORY_ALLOW_LARGE_REINDEX=1` for larger vaults before restart.

On a >2000-note vault you can also rebuild interactively by asking
the bot to call `memory_reindex` (holds the blocking lock for the
duration).

## 5. Recovery: corrupt vault note

Find the offending `.md`, fix or remove the frontmatter, restart the
daemon. Auto-reindex picks up the mtime change and re-ingests the
file. If parse keeps failing, the reindex logs skipped files with a
`reason:` — tail the structlog output to see which note is stuck.

## 6. Vault migration (changing `MEMORY_VAULT_DIR`)

1. **Stop the daemon.** `configure_memory` refuses re-config with a
   different `vault_dir` at runtime; you must kill + restart.
2. `mv` the vault to the new location (or symlink).
3. Update `MEMORY_VAULT_DIR` in `~/.config/0xone-assistant/.env`.
4. Start the daemon. If the old `memory-index.db` is still at the old
   path, delete it — the new vault dir should start with a fresh
   index built from its own contents.

Renaming the vault directory while the daemon is running is UNDEFINED
behaviour; the cached `_CTX["vault_dir"]` still resolves against the
old absolute path, so every read starts returning `CODE_NOT_FOUND`.
Restart.

## 7. Cloud-sync warning

The daemon logs `memory_vault_cloud_sync_path` at WARNING on boot if
the vault sits under `~/Library/Mobile Documents` (iCloud),
`~/Library/CloudStorage`, or `~/Dropbox`. On these prefixes
`fcntl.flock` is silently a no-op (cloud-sync layers don't honour it)
so the write-serialisation guarantee is lost.

If you see the warning: move the vault to a local APFS path and point
`MEMORY_VAULT_DIR` at the new location. Treat the cloud-sync warning
as fatal for data-integrity purposes even though the daemon does not
refuse to start.

## 8. Diagnostics

**Audit log.** Every `mcp__memory__*` tool call produces a JSONL line
in `memory-audit.log`:

```jsonl
{"ts":"2026-04-21T15:20:11.123456+00:00","tool_name":"mcp__memory__memory_write",
 "tool_use_id":"toolu_01...","tool_input":{"path":"inbox/x.md","title":"X","body":"..."},
 "response":{"is_error":false,"content_len":12}}
```

`tool_input` strings are capped at 2048 chars per field (Fix 1); a
truncated value has a `...<truncated>` suffix. Raw body content is
therefore abridged in the audit log — look at the vault file itself
for the full content.

Tail in a separate terminal while smoke-testing:

```bash
tail -f ~/.local/share/0xone-assistant/memory-audit.log | jq .
```

**"Saved but file missing" triage.** Check the audit log for the
`memory_write` tool-use ID. If `response.is_error` is true, the note
was rejected (look up the `code=` in the returned text). If
`is_error` is false but the file isn't on disk, something catastrophic
happened between commit and rename — the transactional ordering in
`write_note_tx` makes this highly unlikely, but if it occurs, the
next auto-reindex repairs the index to match disk reality.

**Singleton lock contention.** If the daemon exits immediately with
`daemon_singleton_lock_held`, another 0xone-assistant is already
running against the same `data_dir`. Find it via:

```bash
cat ~/.local/share/0xone-assistant/.daemon.pid    # prints the holder pid
ps -p $(cat ~/.local/share/0xone-assistant/.daemon.pid)   # systemd-era / native run
```

Stop that one before starting a new instance.

**Docker-era log tailing (phase 5d+):** the bot now runs as a
container; `.daemon.pid` is the container-namespace pid (typically
7 because tini is pid 1). Reading it on the host is meaningless.
Use compose tooling instead:

```bash
cd /opt/0xone-assistant/deploy/docker
docker compose logs -f 0xone-assistant | jq -R 'fromjson?'
docker compose ps
docker compose top
```

The structlog JSON events that previously went to journald now flow
through `docker compose logs`. For systemd-fallback hosts (the unit
in `deploy/systemd/` is retained), the `journalctl --user -u
0xone-assistant -f` recipe still applies.

## 9. Known limitations

- **No audit-log rotation.** Deferred to phase 9. For now the cap is
  the 2 KiB-per-string truncation (Fix 1); expect ~1 MB/month of
  audit log on typical single-user traffic. Manually archive +
  truncate as needed.
- **PyStemmer stem overreach.** Snowball's Russian stemmer collapses
  some homographs (`стекло` glass vs `стекло` verb form) to the same
  stem. Search may return unrelated notes; use the `area` filter and
  read top-3 hits rather than trusting the first one.
- **No sub-vault granularity on `memory_list`.** Lists at most 100
  notes per call. For larger surveys, use `memory_search` with a
  narrow query instead.

## 10. Env var quick-reference (cheat sheet)

```
MEMORY_VAULT_DIR         # absolute path to existing/new vault
MEMORY_INDEX_DB_PATH     # absolute path to FTS5 index DB
MEMORY_MAX_BODY_BYTES    # per-note body cap (default 1048576)
MEMORY_ALLOW_LARGE_REINDEX=1  # opt-out of 2000-note auto-reindex cap
```
