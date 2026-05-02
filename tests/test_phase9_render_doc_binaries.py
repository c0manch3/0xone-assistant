"""Phase 9 — system-binary smoke test for the render_doc subsystem.

Mirrors phase-8 ``test_phase8_ssh_binary_available.py`` — the test
exists so that a regression in the Dockerfile runtime stage (e.g.
dropping pandoc, libpango, or one of the harfbuzz subset libs) is
caught at CI time, not on first live PDF render.

The R-Pandoc + R1.2 verification steps are documented in spec §3
Wave A A3. They run UNCONDITIONALLY when pandoc is on PATH; when
pandoc is missing (Mac dev box without ``brew install pandoc``) the
test SKIPS — owner runs the full smoke inside the ``--target test``
Docker stage where pandoc + WeasyPrint are guaranteed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def test_pandoc_binary_available() -> None:
    """``pandoc`` must resolve via PATH for PDF + DOCX renderers.

    On Mac dev hosts without ``brew install pandoc`` the test SKIPS;
    in the ``--target test`` Docker stage the apt install pulls
    pandoc from Debian bookworm (~164 MiB). Phase 8 ssh-not-found
    incident: 4 reviewer waves + 1014 mocked tests passed before
    a missing system binary broke first live tick. This test gates
    the Docker image build.
    """
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed on this host (CI only)")
    # Smoke: pandoc --version must not crash.
    result = subprocess.run(
        ["pandoc", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"pandoc --version failed: {result.stderr[:256]}"
    )
    assert result.stdout.startswith("pandoc"), (
        "pandoc --version stdout shape unexpected: "
        f"{result.stdout[:64]!r}"
    )


@pytest.mark.requires_pandoc
def test_pandoc_markdown_variant_subtraction_takes_effect() -> None:
    """R-Pandoc closure: ``--list-extensions=markdown-EXT...`` MUST
    show literal ``-EXT`` lines for each subtracted extension AFTER
    subtraction is applied. Closes the trap where pandoc silently
    no-ops on invalid subtraction syntax.
    """
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    variant = (
        "markdown-raw_html-raw_tex-raw_attribute"
        "-tex_math_dollars-tex_math_single_backslash"
        "-yaml_metadata_block"
    )
    result = subprocess.run(
        ["pandoc", f"--list-extensions={variant}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Each subtracted extension MUST appear with leading "-" prefix.
    for ext in (
        "raw_html",
        "raw_tex",
        "raw_attribute",
        "tex_math_dollars",
        "yaml_metadata_block",
    ):
        line = f"-{ext}"
        assert line in result.stdout, (
            f"Subtraction {line} did not take effect; "
            f"output: {result.stdout[:512]}"
        )


def test_weasyprint_url_fetcher_hierarchy() -> None:
    """R1.2 closure: ``FatalURLFetchingError`` MUST extend
    :class:`BaseException` (NOT :class:`IOError`) so WeasyPrint
    propagates the error and aborts render rather than silently
    swallowing it. Sanity-check the hierarchy claim against the
    actually installed WeasyPrint version (pin ``>=63,<70``).
    """
    try:
        from weasyprint.urls import (
            FatalURLFetchingError,
            URLFetcher,
            URLFetchingError,
        )
    except (ImportError, OSError):
        pytest.skip(
            "weasyprint shared libs unavailable on this host "
            "(libpango/libgobject) — CI Docker stage covers this"
        )
    assert issubclass(FatalURLFetchingError, BaseException)
    assert not issubclass(FatalURLFetchingError, IOError)
    # The deprecated variant we explicitly DO NOT use:
    assert issubclass(URLFetchingError, IOError)
    # URLFetcher subclass form is what SafeURLFetcher inherits.
    assert URLFetcher is not None


def test_weasyprint_smoke_probe() -> None:
    """Smoke: ``weasyprint.HTML(string='<p>x</p>').write_pdf(...)``
    must produce a buffer starting with ``%PDF-`` magic bytes. Skips
    on Mac dev hosts without libpango."""
    try:
        import weasyprint  # type: ignore[import-untyped]
    except (ImportError, OSError):
        pytest.skip("weasyprint shared libs unavailable")
    from io import BytesIO

    buf = BytesIO()
    weasyprint.HTML(string="<p>hello</p>").write_pdf(target=buf)
    data = buf.getvalue()
    assert data.startswith(b"%PDF-"), (
        f"WeasyPrint output missing PDF magic; first 32 bytes: "
        f"{data[:32]!r}"
    )
    # PDF spec requires %%EOF trailer.
    assert b"%%EOF" in data[-256:], (
        "WeasyPrint output missing %%EOF trailer"
    )
