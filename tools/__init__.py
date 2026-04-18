"""Tools package — CLI entry points for 0xone-assistant.

Phase-7 tech-debt close (Q9a): tools are proper Python sub-packages so
they can be imported as `tools.<name>._lib.<mod>` from tests and from
their own `main.py` without `sys.path` shims.

Each tool's `main.py` also retains a short `sys.path` pragma so it can
still be launched with `python tools/<name>/main.py` (cwd-relative) in
addition to `python -m tools.<name>.main` (canonical).
"""
