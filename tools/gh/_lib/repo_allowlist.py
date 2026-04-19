"""Repo allow-list helpers used by C3 subcommands and the C4 vault push.

Stdlib-only so the CLI wrapper stays importable without pydantic-settings.
Two public helpers:

- ``extract_owner_repo_from_ssh_url``: parse ``git@github.com:OWNER/REPO.git``
  into the canonical ``"OWNER/REPO"`` slug. Used by C4's vault-commit-push to
  verify ``GH_VAULT_REMOTE_URL`` points at a whitelisted repo before any
  network I/O (I-8.5 defence in depth).
- ``is_repo_allowed``: exact-match membership check against the
  ``GitHubSettings.allowed_repos`` tuple. Used by every C3 handler as the
  first action AFTER argparse returns, BEFORE we shell out to ``gh``.

The regex anchors on the full SSH URL shape and only allows ASCII alnum +
``._-`` inside each segment (matches GitHub's naming rules) so a crafted
remote URL can't smuggle shell metacharacters downstream. This mirrors the
``_GH_SSH_URL_RE`` in ``src/assistant/config.py`` — keeping the two in sync
is explicit because the config module validates configured URLs, whereas
this module parses *any* URL passed at runtime (e.g. as an argparse arg).
"""

from __future__ import annotations

import re

# Full-match anchor. Segments allow the same character class as
# ``_GH_SSH_URL_RE`` in ``src/assistant/config.py`` (ASCII alnum + ``._-``).
# No captures for ``:port`` / ``ssh://`` variants — GitHub's backup convention
# only uses the ``git@github.com:OWNER/REPO.git`` shorthand.
_GH_SSH_URL_RE: re.Pattern[str] = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)\.git$"
)

# Slug shape used by ``gh --repo OWNER/REPO`` invocations. Same character
# class as URL segments; no trailing ``.git``.
_GH_SLUG_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"
)


def extract_owner_repo_from_ssh_url(url: str) -> str:
    """Parse ``git@github.com:OWNER/REPO.git`` → ``"OWNER/REPO"``.

    Raises ``ValueError`` (with the offending input quoted) on any shape
    mismatch. Callers that want a "silent miss" return value should catch
    the exception — we prefer raising because the function is on the
    security-critical path for C4 (allow-list must either succeed with a
    trusted slug or refuse to proceed).
    """
    match = _GH_SSH_URL_RE.match(url)
    if match is None:
        raise ValueError(f"not a recognized GitHub SSH URL: {url!r}")
    return f"{match.group('owner')}/{match.group('repo')}"


def is_repo_allowed(repo: str, allowed: tuple[str, ...]) -> bool:
    """Exact-match membership. ``repo`` must already be ``"OWNER/REPO"`` shape.

    A malformed ``repo`` (anything that does not match the slug regex)
    returns ``False`` even if somehow present in ``allowed`` — this keeps
    the function a monotonic deny-on-weirdness gate. The caller is expected
    to render a clear error JSON; we intentionally don't try to "help" by
    trimming whitespace or lowering case (GitHub slugs are case-sensitive).
    """
    if not _GH_SLUG_RE.match(repo):
        return False
    return repo in allowed


__all__ = ["extract_owner_repo_from_ssh_url", "is_repo_allowed"]
