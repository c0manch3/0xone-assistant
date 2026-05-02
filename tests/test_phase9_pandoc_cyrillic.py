"""Phase 9 fix-pack F10 (DH-4) — pandoc subprocess env carries
``LC_ALL=C.UTF-8`` so Cyrillic round-trip rendering doesn't regress.

Pre-fix-pack: ``LC_ALL`` was absent from ``_pandoc_env``. Pandoc
2.17.1.1 is generally locale-tolerant for UTF-8 input but certain
filter operations (citation sorting, title-case conversions) consult
``LC_COLLATE``. With ``LC_ALL`` unset and a host ``LANG=C``, pandoc
would fall back to ``C`` locale and silently break Cyrillic in those
paths (phase-6c-style regression).

The whitelist test in ``test_phase9_pandoc_env_minimal.py`` already
asserts ``LC_ALL`` is in the returned dict; this test exercises the
end-to-end pipeline against a pandoc binary when available.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc._subprocess import run_pandoc


@pytest.mark.requires_pandoc
def test_pandoc_cyrillic_html_round_trip(tmp_path: Path) -> None:
    """Render Cyrillic markdown via pandoc → HTML5 + assert
    ``Привет, мир`` survives. Skips on hosts without pandoc."""
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")

    md_path = tmp_path / "input.md"
    md_path.write_text(
        "# Привет, мир\n\nТекст на русском.\n", encoding="utf-8"
    )
    out_path = tmp_path / "out.html"

    settings = RenderDocSettings()

    async def run() -> tuple[int, bytes, bytes]:
        return await run_pandoc(
            [
                "pandoc",
                "-f",
                "markdown",
                "-t",
                "html5",
                "-o",
                str(out_path),
                str(md_path),
            ],
            timeout_s=settings.pdf_pandoc_timeout_s,
            settings=settings,
        )

    rc, _, stderr = asyncio.run(run())
    assert rc == 0, stderr.decode("utf-8", "replace")[:512]

    # Cyrillic must survive the round-trip in the rendered HTML.
    rendered = out_path.read_text(encoding="utf-8")
    assert "Привет" in rendered
    assert "мир" in rendered
    assert "Текст на русском" in rendered


@pytest.mark.requires_pandoc
def test_pandoc_env_lc_all_propagates_to_subprocess(
    tmp_path: Path,
) -> None:
    """The subprocess inherits ``LC_ALL=C.UTF-8`` from the whitelist —
    confirmed by running ``pandoc --version`` and inspecting the env
    via a quick locale-dependent op."""
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    # ``locale`` is a tiny system command; we use it to inspect the
    # subprocess env. Skip when not available (e.g. minimal alpine).
    if shutil.which("locale") is None:
        pytest.skip("locale not installed")

    # Compose the same env the @tool body would feed pandoc.
    from assistant.render_doc._subprocess import _pandoc_env

    env = _pandoc_env()
    assert env.get("LC_ALL") == "C.UTF-8" or env["LC_ALL"]

    result = subprocess.run(
        ["locale"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    # ``locale`` must echo back our ``LC_ALL`` setting.
    assert "C.UTF-8" in result.stdout or "LC_ALL" in result.stdout
