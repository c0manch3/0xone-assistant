"""Phase-8 CLI exit codes. Module constants, stdlib-only.

Single source of truth for `tools/gh/main.py` + its unit tests. Values
must stay aligned with `skills/gh/SKILL.md` exit-code matrix and with
the Daemon preflight helpers in `src/assistant/main.py` (wave 5 / C6).
"""

from __future__ import annotations

OK = 0
ARGV = 2
VALIDATION = 3
GH_NOT_AUTHED = 4
NO_CHANGES = 5
REPO_NOT_ALLOWED = 6
DIVERGED = 7
PUSH_FAILED = 8
LOCK_BUSY = 9
SSH_KEY_ERROR = 10

__all__ = [
    "ARGV",
    "DIVERGED",
    "GH_NOT_AUTHED",
    "LOCK_BUSY",
    "NO_CHANGES",
    "OK",
    "PUSH_FAILED",
    "REPO_NOT_ALLOWED",
    "SSH_KEY_ERROR",
    "VALIDATION",
]
