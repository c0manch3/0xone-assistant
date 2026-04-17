"""fetch_bundle refuses private-IP URLs via the SSRF mirror."""

from __future__ import annotations

from pathlib import Path

import pytest

from _lib.fetch import FetchError, fetch_bundle


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/SKILL.md",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
def test_ssrf_gate_rejects_private_or_non_http(url: str, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(FetchError):
        fetch_bundle(url, dest)
    # Ensure no partial write landed on disk.
    assert not dest.exists()


def test_unsupported_url_shape_rejected(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(FetchError, match="unsupported URL"):
        fetch_bundle("https://example.com/not-a-github-url", dest)
    assert not dest.exists()


def test_dest_already_exists_rejected(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(FetchError, match="already exists"):
        fetch_bundle("https://github.com/x/y.git", dest)
