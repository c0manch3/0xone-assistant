"""S-9 — pin env-scrub key sets identical across CLI and Daemon preflight.

The CLI wrapper (``tools/gh/_lib/gh_ops._GH_ENV_SCRUB_KEYS``) and the
Daemon startup preflight
(``src/assistant/main._GH_PREFLIGHT_ENV_SCRUB_KEYS``) must scrub the
same GH / SSH override env variables before calling ``gh auth status``.

A drift between the two would manifest as a preflight success + CLI
failure (or vice versa) depending on which keys were forgotten. This
test pins the two tuples to be element-for-element equal (order-
independent) so any future edit that updates one MUST update the
other, or CI fails.
"""

from __future__ import annotations


def test_gh_env_scrub_key_sets_match() -> None:
    """The CLI and Daemon env-scrub key tuples must contain the same keys."""
    from assistant.main import _GH_PREFLIGHT_ENV_SCRUB_KEYS
    from tools.gh._lib.gh_ops import _GH_ENV_SCRUB_KEYS

    cli_sorted = tuple(sorted(_GH_ENV_SCRUB_KEYS))
    daemon_sorted = tuple(sorted(_GH_PREFLIGHT_ENV_SCRUB_KEYS))
    assert cli_sorted == daemon_sorted, (
        "S-9: env-scrub key drift between CLI (gh_ops) and Daemon "
        "(assistant.main) preflight. Keep the two tuples in sync.\n"
        f"  CLI:    {cli_sorted}\n"
        f"  Daemon: {daemon_sorted}"
    )
