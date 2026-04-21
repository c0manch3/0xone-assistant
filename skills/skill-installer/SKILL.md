---
name: skill-installer
description: "Manage skill installation from GitHub/marketplace. Use when the user wants to preview, install, uninstall, or browse skills."
---

# skill-installer

Installer exposes **seven** SDK custom tools (no CLI). Invoke them
directly as `mcp__installer__<name>`:

- `skill_preview(url)` — fetch + validate a skill bundle, return a
  preview. Trigger phrases: "поставь скилл <url>", "глянь что за
  скилл", "preview this skill".
- `skill_install(url, confirmed)` — install after the user explicitly
  confirms. NEVER invoke with `confirmed=true` without the user saying
  "да" / "yes" in the immediately previous turn.
- `skill_uninstall(name, confirmed)` — remove an installed skill.
- `marketplace_list()` — list Anthropic's official skills. Trigger
  phrases: "какие скилы есть", "что в marketplace".
- `marketplace_info(name)` — show SKILL.md for one marketplace skill.
- `marketplace_install(name)` — shortcut: build the tree-URL for the
  marketplace skill and run the preview pipeline; still requires an
  explicit `skill_install(..., confirmed=true)` in a follow-up turn.
- `skill_sync_status(name)` — check background `uv sync` progress
  after an install that returned `sync_pending: true`.

## Preview-confirm invariant

Never call `skill_install(url=X, confirmed=true)` in the same turn as
the initial `skill_preview(url=X)`. The owner must see the preview
and respond affirmatively in a subsequent message. If the cache entry
has expired (7 days) between preview and install, re-run `skill_preview`.

## Brand-new skills

Creating a skill from scratch is a different job — Anthropic's own
`skill-creator` skill (auto-installed at first daemon boot) handles
that. When the user says "сделай скилл для X", invoke `skill-creator`
guidance instead, which writes files via the built-in `Write` tool.
