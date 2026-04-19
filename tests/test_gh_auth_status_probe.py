"""Probe scenarios for ``tools/gh/main.py auth-status`` (phase-8 C2).

Three shapes covered:

1. ``gh`` logged in → rc=0 from subprocess, exit 0, ``{"ok": true}``.
2. ``gh`` unauthenticated → rc=1 with the canonical "not logged into"
   stderr substring (SF-A6), exit 4, ``{"ok": false, "error": "not_authenticated"}``.
3. ``gh`` not on PATH → ``shutil.which`` returns None, exit 4,
   ``{"ok": false, "error": "gh_not_found"}``.

We monkeypatch ``subprocess.run`` and ``shutil.which`` on the modules
actually used by ``tools.gh._lib.gh_ops`` so the dependency graph is
exercised without hitting the real `gh` binary.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from tools.gh import main as gh_main
from tools.gh._lib import gh_ops


def _fake_run_factory(
    rc: int, stdout: str = "", stderr: str = ""
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a `subprocess.run` replacement returning a canned CompletedProcess.

    The factory ignores positional/keyword args passed by the production
    code (cmd list, env, capture_output, text, timeout) because the probe
    doesn't care about them — the test asserts mapping from rc+stderr to
    CLI exit code, not the exact argv handed to `gh`.
    """

    def _run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["gh", "auth", "status", "--hostname", "github.com"],
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
        )

    return _run


def test_auth_status_ok(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(gh_ops.subprocess, "run", _fake_run_factory(0, stdout="ok\n"))

    rc = gh_main.main(["auth-status"])

    assert rc == 0
    stdout = capsys.readouterr().out.strip()
    assert json.loads(stdout) == {"ok": True}


def test_auth_status_not_authed(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _name: "/usr/bin/gh")
    # Canonical gh unauthenticated stderr — substring "not logged into" is
    # the SF-A6 match used by the Daemon preflight helper (C6). The probe
    # CLI itself only checks rc != 0, but we keep the realistic payload to
    # document expectations.
    monkeypatch.setattr(
        gh_ops.subprocess,
        "run",
        _fake_run_factory(
            1,
            stderr="You are not logged into any GitHub hosts. "
            "To log in, run: gh auth login\n",
        ),
    )

    rc = gh_main.main(["auth-status"])

    assert rc == 4
    stdout = capsys.readouterr().out.strip()
    assert json.loads(stdout) == {"ok": False, "error": "not_authenticated"}


def test_auth_status_gh_missing_on_path(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """`gh` binary absent. The pre-flight `shutil.which(None)` short-circuits
    before `subprocess.run` is ever called; we still install a tripwire on
    `subprocess.run` so an accidental fallthrough fails the test instead
    of hitting the real shell."""

    monkeypatch.setattr(gh_ops.shutil, "which", lambda _name: None)

    def _tripwire(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(
            "subprocess.run must not be invoked when `gh` is not on PATH"
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _tripwire)

    rc = gh_main.main(["auth-status"])

    assert rc == 4
    stdout = capsys.readouterr().out.strip()
    assert json.loads(stdout) == {"ok": False, "error": "gh_not_found"}


def test_build_gh_env_pins_home_to_real_user_home(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """T5.1: ``build_gh_env`` pins ``HOME`` to ``Path.home()`` even when
    the caller's env has ``HOME`` unset or pointing somewhere else.

    Under systemd / launchd, ``HOME`` may be missing or redirected to
    ``/var/empty``; ``gh`` reads ``~/.config/gh/hosts.yml`` via ``HOME``,
    so a wrong value silently breaks auth. The pin uses
    :meth:`pathlib.Path.home` which consults the real uid → pwd mapping
    rather than trusting the inherited env.
    """
    from pathlib import Path

    # Force HOME to a bogus value; build_gh_env must overwrite.
    monkeypatch.setenv("HOME", "/var/empty-should-be-overwritten")
    env = gh_ops.build_gh_env()
    assert env["HOME"] == str(Path.home()), (
        f"T5.1: HOME must be pinned to Path.home(); got {env['HOME']!r}"
    )


def test_build_gh_env_extra_can_still_override_home(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """T5.1: the ``extra`` dict still wins (caller escape hatch).

    If an operator truly wants a specific HOME (e.g. running under a
    service account with a non-standard home), passing it through
    ``extra`` must take precedence over the Path.home() pin. Otherwise
    we'd have a backwards-incompatible contract change.
    """
    env = gh_ops.build_gh_env(extra={"HOME": "/tmp/explicit-override"})
    assert env["HOME"] == "/tmp/explicit-override", (
        "caller-provided HOME must override the Path.home() pin"
    )
