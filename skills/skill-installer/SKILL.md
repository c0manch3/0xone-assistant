---
name: skill-installer
description: "Install new skills from a URL or from the Anthropic marketplace. Invoke when the user asks to add/install a skill, shares a git/github URL pointing at a SKILL.md, or asks what skills are available in the marketplace. Run via Bash: `python tools/skill_installer/main.py <cmd>`."
allowed-tools: [Bash]
---

# skill-installer

Use this skill to **preview and install** other skills. Creating a
brand-new skill from scratch is the job of Anthropic's `skill-creator`
skill (installed automatically at first boot) — invoke that one when the
user wants to design a fresh skill, not this one.

## Commands (all via Bash)

Ad-hoc URL install:

- `python tools/skill_installer/main.py preview <URL>` — fetch bundle,
  run static validation, print a human-readable preview with the bundle
  SHA. The cache is keyed by URL, so running preview twice on the same
  URL is idempotent.
- `python tools/skill_installer/main.py install --confirm --url <URL>` —
  re-fetch the bundle and compare SHA against the cached preview. On
  mismatch, exit code is `7` ("bundle on source changed since preview"
  — ask the user to re-preview). On match, copy files into `skills/`
  (and `tools/` if the bundle carried a top-level `tools/` subdir) and
  touch the manifest-cache sentinel.
- `python tools/skill_installer/main.py status <NAME>` — phase-3 stub;
  returns `{"status": "unknown"}`.

Marketplace (Anthropic's public skills repo, hardcoded):

- `python tools/skill_installer/main.py marketplace list` — JSON list
  of skills under `skills/` in `github.com/anthropics/skills`.
- `python tools/skill_installer/main.py marketplace info <NAME>` —
  print the full `SKILL.md` for one marketplace skill so you can
  summarise it before asking the user to confirm.
- `python tools/skill_installer/main.py marketplace install <NAME>` —
  preview-only by default; add `--confirm` to install.

## Flow

1. User says "install / add skill X" or shares a URL.
2. Run `preview <URL>` (or `marketplace install <NAME>` without
   `--confirm`). Summarise the preview output to the user in Russian —
   name, description, file count, total size, SHA (first 16 chars).
3. Ask the user to confirm in plain text: "Установить? (да/нет)".
4. On "да" / "yes" / "подтверждаю": run
   `install --confirm --url <URL>` (or marketplace command with
   `--confirm`). If exit code is `7` (TOCTOU), ask the user to
   re-preview.
5. On success, the manifest updates automatically on the next turn —
   no daemon restart is needed.

**Preview+confirm is mandatory.** Never call `install` without a prior
`preview` of the same URL — the cache entry is the safety gate.
