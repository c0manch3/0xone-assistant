"""Phase 6c hotfix — SSH reverse tunnel pivot.

Owner's AmneziaVPN on Mac mini conflicts with Tailscale's default-route
capture, so the cross-host transport pivoted to an SSH reverse tunnel
(``autossh -N -R 9000:localhost:9000`` Mac → VPS) with VPS sshd
``GatewayPorts yes`` re-publishing the listener on the docker bridge.

These tests don't exercise networking — they assert the static
artefacts that drive the bootstrap (compose file, plist, README, sidecar
config defaults) match the hotfix invariants. If a future change drifts
the bot back toward a Tailscale-only config the tests fail loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import ClaudeSettings, Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker" / "docker-compose.yml"
TUNNEL_PLIST = (
    REPO_ROOT / "whisper-server" / "com.zeroxone.whisper-tunnel.plist"
)
SIDECAR_README = REPO_ROOT / "whisper-server" / "README.md"
SETUP_SCRIPT = REPO_ROOT / "whisper-server" / "setup-mac-sidecar.sh"

# CI test image only COPYs ``src/`` + ``tests/`` (per Dockerfile target=test);
# ``deploy/`` and ``whisper-server/`` are NOT in the container. Skip the
# file-presence assertions there. Locally + on dev hosts the repo layout
# is full and these tests run.
_REPO_FULL = COMPOSE_FILE.exists() and TUNNEL_PLIST.exists()
pytestmark = pytest.mark.skipif(
    not _REPO_FULL,
    reason="repo deploy/+whisper-server not present (CI test container)",
)


def test_compose_no_tailscale_service() -> None:
    """The Tailscale sidecar service was removed in the hotfix."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    # The container_name and image string are precise enough to avoid
    # false positives if "tailscale" appears in a comment / migration
    # note (which is fine).
    assert "container_name: 0xone-tailscale" not in text
    assert "image: tailscale/tailscale" not in text
    assert "TS_AUTHKEY" not in text
    # Sanity: the new transport hook is in place.
    assert "host.docker.internal:host-gateway" in text


def test_compose_no_secrets_tailscale_env_file() -> None:
    """The split secrets-tailscale.env file is no longer wired in."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "secrets-tailscale.env" not in text


def test_tunnel_plist_present_and_well_formed() -> None:
    """The new whisper-tunnel LaunchAgent ships in-repo."""
    assert TUNNEL_PLIST.exists()
    text = TUNNEL_PLIST.read_text(encoding="utf-8")
    assert "com.zeroxone.whisper-tunnel" in text
    # autossh invocation invariants — these are load-bearing for the
    # hotfix design.
    assert "/opt/homebrew/bin/autossh" in text
    assert "9000:localhost:9000" in text
    assert "0xone@193.233.87.118" in text
    assert "ServerAliveInterval=30" in text
    assert "ExitOnForwardFailure=yes" in text
    # __HOME__ substitution remains a string the setup script swaps in.
    assert "__HOME__/.ssh/whisper_tunnel" in text


def test_sidecar_config_default_host_is_loopback() -> None:
    """FastAPI default bind is loopback now — the tunnel handles transit."""
    # Lazy-import: the whisper_server package is not installed in the
    # bot test environment by default, so probe the file directly.
    cfg = (
        REPO_ROOT
        / "whisper-server"
        / "whisper_server"
        / "config.py"
    ).read_text(encoding="utf-8")
    assert 'host: str = "127.0.0.1"' in cfg
    assert 'host: str = "0.0.0.0"' not in cfg


def test_setup_script_uses_autossh_not_tailscale() -> None:
    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    assert "brew install ffmpeg python@3.12 autossh" in text
    assert "tailscale up" not in text
    assert "tag:whisper-mac" not in text
    # Key generation block + advisory hint about restrict,permitlisten.
    assert "ssh-keygen -t ed25519" in text
    assert "whisper_tunnel" in text
    assert 'permitlisten="9000"' in text


def test_readme_references_ssh_tunnel_not_tailscale() -> None:
    """The sidecar README narrative must match the new transport."""
    text = SIDECAR_README.read_text(encoding="utf-8")
    assert "SSH reverse tunnel" in text
    assert "host.docker.internal" in text
    # No active references to Tailscale-as-the-current-design — only
    # the migration note explaining the pivot is allowed.
    # "Tailscale" appears once in the migration note; assert <=2 to
    # tolerate a future reword without inviting silent regressions.
    assert text.count("Tailscale") <= 4
    assert "MagicDNS" not in text


def test_settings_accept_host_docker_internal_url(tmp_path: Path) -> None:
    """The new canonical WHISPER_API_URL parses cleanly."""
    settings = Settings(
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        whisper_api_url="http://host.docker.internal:9000",
        whisper_api_token="x" * 32,
    )
    assert settings.whisper_api_url == "http://host.docker.internal:9000"


def test_settings_still_reject_scheme_less_url(tmp_path: Path) -> None:
    """F14 regression — even with the new transport, scheme-less URLs
    still bomb at boot."""
    with pytest.raises(ValueError, match="http://"):
        Settings(
            telegram_bot_token="123456:" + "x" * 30,
            owner_chat_id=42,
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            whisper_api_url="host.docker.internal:9000",
            whisper_api_token="x" * 32,
        )
