"""SSRF guard in installer core — layer-2 against _ip_is_blocked."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def configured_installer(tmp_path: Path) -> tuple[Path, Path]:
    from assistant.tools_sdk.installer import configure_installer

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "skills").mkdir()
    (project_root / "tools").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    configure_installer(project_root=project_root, data_dir=data_dir)
    return project_root, data_dir


def test_check_host_safety_blocks_literal_ipv4() -> None:
    from assistant.tools_sdk import _installer_core as core

    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254"):
        with pytest.raises(core.URLError, match="SSRF"):
            core.check_host_safety(ip)


def test_check_host_safety_blocks_literal_ipv6() -> None:
    from assistant.tools_sdk import _installer_core as core

    for ip in ("::1", "fe80::1", "fc00::1"):
        with pytest.raises(core.URLError, match="SSRF"):
            core.check_host_safety(ip)


def test_check_host_safety_empty_host() -> None:
    from assistant.tools_sdk import _installer_core as core

    with pytest.raises(core.URLError, match="empty host"):
        core.check_host_safety("")


def test_check_host_safety_dns_private() -> None:
    """Layer-2: public hostname that resolves to private IP → block."""
    from assistant.tools_sdk import _installer_core as core

    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 443))]
    with (
        patch("socket.getaddrinfo", return_value=fake_dns),
        pytest.raises(core.URLError, match="SSRF"),
    ):
        core.check_host_safety("innocent.example")


def test_check_host_safety_dns_public_allows() -> None:
    from assistant.tools_sdk import _installer_core as core

    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
    with patch("socket.getaddrinfo", return_value=fake_dns):
        # Should not raise.
        core.check_host_safety("dns.google")


async def test_skill_preview_ssrf_ipv4(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_preview

    result = await skill_preview.handler({"url": "http://169.254.169.254/"})
    assert result.get("is_error") is True
    # CODE_URL_BAD (1) if unsupported scheme; CODE_SSRF (4) otherwise.
    # http:// is not in the scheme whitelist, so it's CODE_URL_BAD.
    assert result["code"] == 1


async def test_skill_preview_ssrf_ipv6(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_preview

    # https with IPv6 literal URL form.
    result = await skill_preview.handler({"url": "https://[fe80::1]/repo"})
    assert result.get("is_error") is True
    assert result["code"] == 4  # CODE_SSRF
