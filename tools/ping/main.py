"""Healthcheck tool — stdlib-only per Q10 (no per-tool venv for a 10-line smoke)."""

from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdout.write(json.dumps({"pong": True}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
