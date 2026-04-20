from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Settings-file guard (SW3, v2.2).
#
# ``.claude/settings*.json`` fields are split into three categories:
#
#   BLOCK at startup (sys.exit(3)) — override our PreToolUse guards:
#     - top-level ``hooks``: the SDK would install user hooks that may
#       short-circuit ours (our matchers are ``Bash``/``Read``/…/``WebFetch``;
#       a catch-all hook with ``"permissionDecision":"allow"`` wins).
#     - ``permissions.deny``: different semantics (SDK enforces these before
#       hooks fire — behaviour change).
#     - ``permissions.defaultMode``: alters allow/ask/deny baseline.
#     - ``permissions.additionalDirectories``: widens file-tool scope.
#
#   WARN (startup continues) — benign user-level Claude Code CLI grants that
#   do NOT interact with our in-process bridge:
#     - ``permissions.allow`` / ``permissions.ask``: interactive-CLI grants.
#       The SDK session we spawn uses its own permissions plumbed through
#       ``ClaudeAgentOptions`` + our PreToolUse hooks.
#     - ``statusLine`` / ``theme`` / ``defaultModel`` / ``mcpServers`` / etc.
#
# Rationale: the previous policy (block on ANY ``permissions`` key) was too
# aggressive — the owner's dev workstation routinely has a user-level
# ``permissions.allow`` from day-to-day Claude Code CLI use, producing a
# confusing ``exit(3)`` on first ``just run``. We now discriminate keys that
# actually affect our bridge from benign user-level grants.
# ---------------------------------------------------------------------------
_BLOCKED_PERMISSIONS_KEYS: frozenset[str] = frozenset(
    {"deny", "defaultMode", "additionalDirectories"}
)


def ensure_skills_symlink(project_root: Path) -> None:
    """Idempotently create ``.claude/skills -> <project_root>/skills``.

    S4 fix: use an absolute symlink target so the link stays valid even if
    the process ``chdir``'s away. If a link with a different target
    (e.g. the old relative ``../skills`` from a prior rebuild) is present,
    replace it.
    """
    link = project_root / ".claude" / "skills"
    link.parent.mkdir(exist_ok=True)
    target = (project_root / "skills").resolve()
    if link.is_symlink():
        # ``readlink()`` returns whatever was stored — the raw target path,
        # which may be absolute or relative. S4 requires an absolute stored
        # target so the link stays valid across chdir; a link whose stored
        # target is relative is stale even if it resolves to the same
        # absolute path, and must be rewritten.
        try:
            stored = link.readlink()
            resolved = (link.parent / stored).resolve()
        except OSError:
            stored = None
            resolved = None
        if stored is not None and stored.is_absolute() and resolved == target:
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(f".claude/skills exists and is not a symlink: {link}")
    link.symlink_to(target, target_is_directory=True)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (
                "<REDACTED>"
                if any(s in k.lower() for s in ("token", "secret", "key", "password"))
                else _redact(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _analyze_settings(content: dict[str, Any]) -> list[str]:
    """SW3: return a list of block-worthy reasons for this settings file.

    Empty list → safe to continue (optionally with a soft warning if there
    are benign user-level grants). Non-empty → caller aborts with
    ``sys.exit(3)`` and prints these reasons so the owner knows what to fix.
    """
    reasons: list[str] = []
    if "hooks" in content:
        reasons.append(
            "top-level 'hooks' — would install user hooks alongside ours and "
            "can short-circuit PreToolUse guards (a catch-all 'allow' wins)."
        )
    perms = content.get("permissions")
    if isinstance(perms, dict):
        for key in sorted(_BLOCKED_PERMISSIONS_KEYS & perms.keys()):
            reasons.append(
                f"'permissions.{key}' — changes SDK permission semantics. "
                "Move to bridge/hooks.py or ClaudeAgentOptions instead."
            )
    return reasons


def _has_benign_user_grants(content: dict[str, Any]) -> bool:
    """True iff the file carries user-level Claude Code CLI grants we warn
    about but do not block on (``permissions.allow`` / ``permissions.ask``).
    """
    perms = content.get("permissions")
    if not isinstance(perms, dict):
        return False
    return bool(perms.get("allow")) or bool(perms.get("ask"))


def assert_no_custom_claude_settings(project_root: Path, logger: logging.Logger) -> None:
    """Guard against ``.claude/settings*.json`` silently overriding our hooks.

    SW3 policy (v2.2):
      - BLOCK (``sys.exit(3)``) on keys that actually change SDK behaviour:
        top-level ``hooks`` or ``permissions.{deny,defaultMode,
        additionalDirectories}``.
      - WARN (startup continues) on benign user-level CLI grants
        (``permissions.allow`` / ``permissions.ask``) and on cosmetic keys
        (``statusLine``, ``theme``, ``defaultModel``, ``mcpServers`` …).

    Rationale: ``setting_sources=["project"]`` pulls ``.claude/settings.json``
    into CLI config resolution. The blocked subset would silently bypass or
    alter our PreToolUse guards; user-level ``allow``/``ask`` grants live in
    the interactive CLI surface and do not cross into our bridge session.
    """
    for name in ("settings.json", "settings.local.json"):
        path = project_root / ".claude" / name
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse .claude/%s: %s", name, exc)
            continue
        if not isinstance(raw, dict):
            logger.warning(".claude/%s is not a JSON object — ignored.", name)
            continue
        block_reasons = _analyze_settings(raw)
        if block_reasons:
            logger.error(
                "claude_settings_conflict",
                extra={
                    "file": name,
                    "block_reasons": block_reasons,
                    "hint": (
                        f"Remove or migrate the offending keys from .claude/{name}. "
                        "Hooks and permission semantics MUST live in bridge/hooks.py "
                        "(phase 2 baseline). Startup aborted to prevent silent "
                        "hook bypass."
                    ),
                },
            )
            sys.exit(3)
        if _has_benign_user_grants(raw):
            logger.warning(
                ".claude/%s contains user-level 'permissions.allow'/'ask' grants — "
                "allowed (these do not affect the in-process SDK session). "
                "Content (redacted): %s",
                name,
                _redact(raw),
            )
        else:
            logger.warning(
                ".claude/%s present — allowed (no blocking keys). Content (redacted): %s",
                name,
                _redact(raw),
            )
