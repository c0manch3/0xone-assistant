---
name: vault
description: 'Manual push of vault → GitHub. ONLY for explicit owner request ("запушь вольт", "сделай бэкап заметок", "синхронизируй vault", "push vault now"). Auto-sync runs hourly without intervention.'
allowed-tools: ["mcp__vault__vault_push_now"]
---

# Vault sync

Vault syncs to GitHub automatically every hour (cron). Owner can also
trigger an immediate push via `vault_push_now` MCP tool.

## When to use

- Owner explicitly says "запушь вольт", "сделай бэкап", "синхронизируй
  vault now" or similar.
- Owner explicitly asks for an immediate sync.

## When NOT to use

- After every `memory_write` — auto-sync covers this. Do NOT chain
  `memory_write → vault_push_now` as a side-effect.
- Without explicit owner request — pre-rate-limit (60s window) prevents
  abuse, but this is defence-in-depth, not policy.

## Rate limit

`vault_push_now` enforces 60s minimum interval between successful
invocations. A second call inside the window returns `{"ok": false,
"reason": "rate_limit", "next_eligible_in_s": N}`.

## Failure modes

- `not_configured` → vault_sync disabled in settings.
- `lock_contention` → vault_lock held by memory_write; retry shortly.
- `failed` → push to GitHub failed (e.g. divergence). Owner gets
  Telegram notify.
