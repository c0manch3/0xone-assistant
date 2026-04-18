"""Internal helpers for the skill_installer CLI.

This package is stdlib-only (phase-3 B-4 decision). No httpx, no pyyaml —
the installer runs under the main interpreter via
`python tools/skill_installer/main.py <cmd>` and imports only from the
stdlib and from its own `_lib/` tree.
"""
