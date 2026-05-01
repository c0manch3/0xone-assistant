"""Phase 8 W2-H4: secret denylist validation against staged vault paths.

The same regex set is used by both this daemon-side check and the
``deploy/scripts/vault-bootstrap.sh`` pre-push validation. Daemon-side
matching is :func:`re.search` against vault-relative paths produced by
``git diff --cached --name-only``; that contract is mirrored verbatim
in the bootstrap script with an inline comment marking the dependency.

The single source of truth for the regex set lives in
:attr:`assistant.config.VaultSyncSettings.secret_denylist_regex`.
This module accepts the patterns as a parameter so tests can
parametrise without monkey-patching the settings object.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


def validate_no_secrets(
    staged_files: Sequence[str],
    regex_patterns: Sequence[str],
) -> list[str]:
    """Return a list of staged paths that match any denylist regex.

    Empty list = OK (commit may proceed). Non-empty = block; the caller
    surfaces a structured log + Telegram notify and refuses to invoke
    ``git commit``.

    Each pattern is compiled once per call. The patterns are static
    config (six entries by default) so the compile cost is negligible
    and pre-compiling at module import would couple this helper to the
    settings instance, which complicates testability.
    """
    if not staged_files:
        return []
    compiled = [re.compile(p) for p in regex_patterns]
    matches: list[str] = []
    for path in staged_files:
        for rgx in compiled:
            if rgx.search(path):
                matches.append(path)
                break
    return matches
