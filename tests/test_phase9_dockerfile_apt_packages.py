"""Phase 9 Wave D — static check that Dockerfile runtime stage carries
the R1.4 apt list.

Mirrors phase-8's dockerfile static checks. R1.4 closure: ``libcairo2``
and ``libgdk-pixbuf2.0-0`` MUST be absent (WeasyPrint v53+ no longer
needs them); ``libharfbuzz-subset0`` MUST be present (PDF font
subsetting).
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCKERFILE = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "docker"
    / "Dockerfile"
)


def test_dockerfile_runtime_apt_list_has_required_packages() -> None:
    """R1.4 closure — required pkgs present, deprecated absent."""
    if not DOCKERFILE.exists():
        pytest.skip(
            f"Dockerfile not present at {DOCKERFILE} "
            "(test container only COPYs src/ + tests/)"
        )
    text = DOCKERFILE.read_text(encoding="utf-8")
    required = (
        "pandoc",
        "libpango-1.0-0",
        "libpangoft2-1.0-0",
        "libharfbuzz-subset0",
        "fonts-dejavu-core",
    )
    for pkg in required:
        assert pkg in text, (
            f"Dockerfile missing apt pkg {pkg!r}; runtime stage will "
            "fail render_doc smoke at first use"
        )


def test_dockerfile_does_not_carry_deprecated_apt_pkgs() -> None:
    """R1.4: ``libcairo2`` and ``libgdk-pixbuf2.0-0`` removed from
    direct apt list (WeasyPrint v53+ doesn't need them)."""
    if not DOCKERFILE.exists():
        pytest.skip(
            f"Dockerfile not present at {DOCKERFILE} "
            "(test container only COPYs src/ + tests/)"
        )
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Allow indirect/transitive (apt may pull libcairo2 via
    # libpangocairo-1.0-0). Only guard against EXPLICIT apt-install.
    deprecated = ("libcairo2", "libgdk-pixbuf2.0-0")
    # Find the runtime apt-install block. Crude: any line
    # ``        libcairo2 \`` directly under the runtime
    # apt-get install would surface here. Match exact-token form.
    for pkg in deprecated:
        # Permit appearance inside comments. Reject only if the
        # package name appears as a backslash-continued install line.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            tokens = stripped.split()
            if pkg in tokens and stripped.endswith("\\"):
                raise AssertionError(
                    f"Dockerfile carries deprecated apt pkg {pkg!r} "
                    f"on line: {line.strip()}"
                )
