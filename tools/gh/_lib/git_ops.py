"""Git subprocess wrappers for ``vault-commit-push`` (path-pinned, env-controlled).

All functions here accept an explicit ``vault_dir`` and pass it via ``git -C``,
so the caller's cwd never leaks into the command line. Environment is
sourced from ``_base_env`` which scrubs ``GH_*`` tokens plus
``GIT_SSH_COMMAND`` / ``SSH_ASKPASS`` (SF-C2/SF-C3) so a stale dev-shell
env never leaks into the daemon's commits.

Public surface used by ``tools/gh/main.py::_cmd_vault_commit_push``:

- :data:`DIVERGED_RE` — stderr regex classifying non-fast-forward / fetch-first
  / rejected / updates-were-rejected markers emitted by ``git push``. Used by
  :func:`push` to decide between exit 7 (``DIVERGED``) and exit 8 (``PUSH_FAILED``).
- :class:`GitResult` — small dataclass carrying ``rc`` / ``stdout`` / ``stderr``.
- :func:`is_inside_work_tree` / :func:`porcelain_status` / :func:`stage_all` /
  :func:`commit` — the usual staged-change flow.
- :func:`unpushed_commit_count` — B-B2 helper. Returns how many local commits
  are ahead of the configured upstream ``@{u}``. Used BEFORE the porcelain
  check to detect "prior run committed but push failed" situations, so we
  can retry a push-only cycle without duplicating commits.
- :func:`reset_soft_head_one` — B-B2 helper. Called when ``push()`` returns
  ``"diverged"``; undoes the local commit while keeping the index + working
  tree intact so the next run can re-try cleanly.
- :func:`diff_cached_empty` — ``git diff --cached --quiet`` wrapper, used as
  a post-``add`` race-window recheck (something may have been reverted
  between the porcelain call and ``git add``).
- :func:`files_changed_count` — walks ``git show --name-only HEAD`` for the
  commit payload we just made so the success JSON can include
  ``"files_changed": N``.
- :func:`push` — the only function that sets ``GIT_SSH_COMMAND`` (constructed
  via ``shlex.quote`` per spike R-12). Returns ``(GitResult, "ok" | "diverged"
  | "failed")``.

Invariant I-8.4: ``commit`` uses **inline** ``-c user.email=X -c user.name=Y``
(MUST precede the git subcommand — ``-c`` on ``commit`` means "use this
commit as a template", a different operation) so we never mutate the
caller's global git config. See spike R-7 for argv semantics.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Divergence classifier.
#
# Spike R-14 / R-8 produced four stderr markers from real `git push` output
# on divergent branches (local-bare-repo simulation):
#
#   ! [rejected]        main -> main (fetch first)
#   error: failed to push some refs to '...'
#   hint: Updates were rejected because the remote contains work...
#   non-fast-forward
#
# We case-insensitive-match ANY of `rejected`, `non-fast-forward`,
# `fetch first`, `updates were rejected`. The `\b` word boundaries keep us
# from matching `rejected_` or similar substrings inside a path/URL.
DIVERGED_RE: re.Pattern[str] = re.compile(
    r"\b(rejected|non-fast-forward|fetch first|updates were rejected)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GitResult:
    """Captured result of a single ``git`` subprocess call.

    Frozen dataclass so tests can compare by equality and handlers can
    pass the object around without worrying about aliasing. ``stdout`` /
    ``stderr`` are always ``str`` (the wrapper forces ``text=True``); an
    empty capture is ``""``, never ``None``.
    """

    rc: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# Env scrubbing for every `git` invocation we make.

# SF-C2 / SF-C3: every variant of `GH_*` / enterprise token / host / config-dir
# / ssh-override must be removed BEFORE we run `git` so the subprocess never
# inherits a stale (potentially hostile) dev-shell env. `SSH_AUTH_SOCK` is
# deliberately kept — our `push()` sets `IdentitiesOnly=yes` in
# `GIT_SSH_COMMAND` so openssh will ignore any agent identities, but we
# still want unrelated ssh tooling downstream to work if the operator
# launches `git log` etc. from the same shell.
_GIT_ENV_SCRUB_KEYS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
    "GIT_SSH_COMMAND",  # set explicitly by `push()` when relevant
    "SSH_ASKPASS",      # avoid password-prompt popups in headless daemon
)


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a scrubbed env dict with ``GIT_TERMINAL_PROMPT=0`` forced.

    Callers that need to set `GIT_SSH_COMMAND` (only :func:`push`) pass it
    via ``extra`` so the scrub/set ordering is deterministic (scrub first,
    then overlay extras). ``GIT_TERMINAL_PROMPT=0`` guarantees that a
    misconfigured credential helper never blocks the daemon waiting for
    stdin input.
    """
    env = dict(os.environ)
    for key in _GIT_ENV_SCRUB_KEYS:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 30.0,
    check: bool = False,
) -> GitResult:
    """Run ``git -C <cwd> <args>`` with env-scrub; return :class:`GitResult`.

    ``check=True`` raises :class:`RuntimeError` on non-zero rc, with stderr
    included so the handler can bubble the failure to the CLI JSON. We
    deliberately do NOT raise :class:`subprocess.CalledProcessError` —
    that class's ``__str__`` includes the argv which may contain
    sensitive tokens; our ``RuntimeError`` message is curated.
    """
    proc = subprocess.run(  # noqa: S603 — `git` is a trusted binary, argv is validated
        ["git", "-C", str(cwd), *args],
        env=env if env is not None else _base_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args!r} failed rc={proc.returncode} stderr={proc.stderr[:200]!r}"
        )
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def is_inside_work_tree(vault_dir: Path) -> bool:
    """True iff ``vault_dir`` is inside a git work-tree (has a `.git` ancestor).

    Used by the CLI to decide whether to call :mod:`vault_git_init.bootstrap`
    on first run. Returns False on a fresh mkdir, on a dir that was just
    ``rm -rf``'d, or on any other non-git directory.
    """
    r = _run_git(["rev-parse", "--is-inside-work-tree"], vault_dir)
    return r.rc == 0 and r.stdout.strip() == "true"


def porcelain_status(vault_dir: Path) -> str:
    """Return ``git status --porcelain`` stdout verbatim.

    Spike R-9 confirmed: `--porcelain` (v1) is the only reliable "any
    change?" detector, because `git diff --quiet` misses untracked files
    entirely (B2 blocker). Non-empty stdout means something must be staged;
    empty stdout means ``exit 5 no_changes``.
    """
    r = _run_git(["status", "--porcelain"], vault_dir, check=True)
    return r.stdout


def stage_all(vault_dir: Path) -> None:
    """Run ``git add -A`` inside ``vault_dir``.

    Since vault_dir is a standalone repo (Q9 topology), ``-A`` only picks
    up paths inside vault_dir — the project-root tree is out of scope by
    construction. I-8.1 belt-and-suspenders is provided by :func:`commit`
    which also uses ``--only -- .``.
    """
    _run_git(["add", "-A"], vault_dir, check=True)


def commit(
    vault_dir: Path,
    *,
    message: str,
    author_email: str,
    author_name: str = "vaultbot",
) -> str:
    """Create a commit via inline ``-c user.email=X -c user.name=Y``.

    Returns the new HEAD sha.

    SF-A4 / spike R-7: ``-c KEY=VALUE`` MUST precede the subcommand. So the
    final argv is ``git -C <vault_dir> -c user.email=... -c user.name=...
    commit --only -m <msg> -- .``. Putting ``-c`` AFTER ``commit`` would
    select that commit as a template — a totally different operation that
    would silently overwrite the owner's intended message.

    ``--only -- .`` is a defence-in-depth for I-8.1: even if something
    else accidentally staged a path outside vault_dir (cannot happen in
    the Q9 topology, but still), ``--only`` restricts the commit payload
    to paths matching the pathspec.
    """
    _run_git(
        [
            "-c", f"user.email={author_email}",
            "-c", f"user.name={author_name}",
            "commit", "--only", "-m", message, "--", ".",
        ],
        vault_dir,
        check=True,
    )
    head = _run_git(["rev-parse", "HEAD"], vault_dir, check=True)
    return head.stdout.strip()


def unpushed_commit_count(vault_dir: Path) -> int:
    """B-B2: number of commits on HEAD not reachable from upstream ``@{u}``.

    Returns 0 if the branch has no configured upstream yet (``@{u}``
    resolution fails with rc != 0) or if ``rev-list`` output is
    unparseable — both of those cases are treated as "nothing to retry"
    so a first-ever push goes through the normal stage-and-commit path.

    Used BEFORE the porcelain change-detection step in
    ``_cmd_vault_commit_push``: if a previous run managed to commit
    locally but failed to push (network blip, stderr didn't classify as
    "diverged"), the unpushed count will be ``>= 1`` and we retry push
    WITHOUT creating a new commit so there's no duplicate / dangling
    parent chain.
    """
    r = _run_git(["rev-list", "@{u}..HEAD", "--count"], vault_dir)
    if r.rc != 0:
        # Upstream unknown (first push) or rev-list error. Both map to
        # "nothing unpushed" — the caller will proceed to the normal
        # stage-and-commit path, and the push itself is the check.
        return 0
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        # Defensive: `git rev-list --count` emits a bare integer, but if
        # some future git version changes the format we prefer 0 over a
        # crash so the owner's data still gets through the stage-commit
        # path.
        return 0


def reset_soft_head_one(vault_dir: Path) -> None:
    """B-B2: undo the most recent commit while keeping the index+tree intact.

    Called when :func:`push` returns ``"diverged"`` during the normal
    stage-commit-push cycle. Effect: HEAD moves back one commit, the
    previously-committed changes remain staged (tree is still dirty), so
    the NEXT run will re-detect them via porcelain and try a fresh
    commit+push against (presumably by then) up-to-date remote.

    Contract: this is ONLY safe after a commit we OWN (just made). Never
    call it on a commit that may already exist upstream.
    """
    _run_git(["reset", "--soft", "HEAD~1"], vault_dir, check=True)


def diff_cached_empty(vault_dir: Path) -> bool:
    """Return True iff ``git diff --cached --quiet`` returns rc=0 (nothing staged).

    Race-window recheck: between :func:`porcelain_status` reporting "dirty"
    and :func:`stage_all`, another process could revert the tree to match
    HEAD (unlikely but cheap to handle). If `diff --cached --quiet` is 0
    AFTER ``git add -A``, there's genuinely nothing to commit and we exit
    5 ``no_changes``.
    """
    r = _run_git(["diff", "--cached", "--quiet"], vault_dir)
    return r.rc == 0


def files_changed_count(vault_dir: Path) -> int:
    """Count the files listed in ``git show --name-only HEAD``.

    Used for the ``"files_changed": N`` field in the success JSON. We
    filter empty lines out of the output (``--pretty=format:`` still
    emits a leading empty line to separate header from names).
    """
    r = _run_git(
        ["show", "--name-only", "--pretty=format:", "HEAD"], vault_dir
    )
    return len([line for line in r.stdout.splitlines() if line.strip()])


def push(
    vault_dir: Path,
    *,
    remote: str,
    branch: str,
    ssh_key_path: Path,
    known_hosts_path: Path,
) -> tuple[GitResult, str]:
    """Push ``branch`` to ``remote`` over isolated ssh; classify the outcome.

    Returns ``(GitResult, verdict)`` where ``verdict`` is one of:

    - ``"ok"`` — rc=0. Caller emits exit 0.
    - ``"diverged"`` — rc != 0 AND stderr matches :data:`DIVERGED_RE`.
      Caller emits exit 7 after :func:`reset_soft_head_one`.
    - ``"failed"`` — rc != 0 with any other stderr. Caller emits exit 8.

    Spike R-12 mandate: every non-literal path in ``GIT_SSH_COMMAND`` goes
    through :func:`shlex.quote`. Without it, a path containing a space
    would be word-split by git's POSIX-shell parser, and a path with
    embedded `` -o `` would let an attacker inject arbitrary ssh
    options. The config validator rejects such paths at load time; the
    shlex.quote here is belt-and-suspenders.

    ``StrictHostKeyChecking=accept-new`` is the TOFU mode: first connection
    pins the host key into the isolated ``UserKnownHostsFile``; later
    connections require an exact match (so an MITM attempting to swap the
    key post-pin will fail loudly). The known-hosts file lives under
    ``<data_dir>/run/`` so neither the owner's ``~/.ssh/known_hosts`` nor
    the system file is ever touched.

    ``IdentitiesOnly=yes`` is the anti-agent-confusion setting: openssh
    is told to use ONLY the ``-i`` key, ignoring any
    ``SSH_AUTH_SOCK``-provided identities. This is the reason
    :data:`_GIT_ENV_SCRUB_KEYS` can safely leave ``SSH_AUTH_SOCK`` in
    place.
    """
    ssh_cmd = " ".join(
        [
            "ssh",
            "-i", shlex.quote(str(ssh_key_path)),
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={shlex.quote(str(known_hosts_path))}",
        ]
    )
    env = _base_env({"GIT_SSH_COMMAND": ssh_cmd})
    r = _run_git(
        ["push", remote, branch], vault_dir, env=env, timeout_s=60.0
    )
    if r.rc == 0:
        return r, "ok"
    if DIVERGED_RE.search(r.stderr):
        return r, "diverged"
    return r, "failed"


__all__ = [
    "DIVERGED_RE",
    "GitResult",
    "commit",
    "diff_cached_empty",
    "files_changed_count",
    "is_inside_work_tree",
    "porcelain_status",
    "push",
    "reset_soft_head_one",
    "stage_all",
    "unpushed_commit_count",
]
