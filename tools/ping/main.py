"""ping tool — prints a single JSON line `{"pong": true}`.

Smoke test for the skill-discovery pipeline. Stdlib-only; no venv required.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdout.write(json.dumps({"pong": True}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
