"""Marketplace list/info wrapper handles the `gh` rc=0 on 404 surprise."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

import tools.skill_installer._lib.marketplace as mkt


class _FakeProc:
    def __init__(self, rc: int, stdout: str, stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch subprocess.run + shutil.which used inside marketplace."""
    state: dict[str, Any] = {"calls": []}

    def _run(cmd: list[str], *a: Any, **kw: Any) -> _FakeProc:
        state["calls"].append(cmd)
        return state["next"]

    monkeypatch.setattr(mkt, "shutil", _FakeShutil())
    monkeypatch.setattr(mkt.subprocess, "run", _run)
    return state


class _FakeShutil:
    """Tiny stand-in so `shutil.which("gh")` returns a fake path."""

    @staticmethod
    def which(name: str) -> str | None:
        return "/fake/gh" if name == "gh" else None


def test_list_filters_dirs_and_dotfiles(fake_run: dict[str, Any]) -> None:
    fake_run["next"] = _FakeProc(
        rc=0,
        stdout=json.dumps(
            [
                {"name": "skill-creator", "type": "dir", "path": "skills/skill-creator"},
                {"name": "pdf", "type": "dir", "path": "skills/pdf"},
                {"name": ".gitattributes", "type": "file", "path": "skills/.gitattributes"},
                {"name": ".ci", "type": "dir", "path": "skills/.ci"},
            ]
        ),
    )
    entries = mkt.list_skills()
    assert [e["name"] for e in entries] == ["skill-creator", "pdf"]


def test_list_raises_on_gh_rc0_404(fake_run: dict[str, Any]) -> None:
    # spike S2.d: `gh api` rc=0 + `{message, status}` payload.
    fake_run["next"] = _FakeProc(rc=0, stdout=json.dumps({"message": "Not Found", "status": "404"}))
    with pytest.raises(mkt.MarketplaceError, match="404"):
        mkt.list_skills()


def test_list_raises_on_gh_failure_stderr(fake_run: dict[str, Any]) -> None:
    fake_run["next"] = _FakeProc(rc=1, stdout="", stderr="auth token expired")
    with pytest.raises(mkt.MarketplaceError, match="rc=1"):
        mkt.list_skills()


def test_parse_gh_json_skips_leading_warnings() -> None:
    out = 'warning: gh is out of date\n[{"name": "pdf", "type": "dir"}]\n'
    assert mkt._parse_gh_json(out) == [{"name": "pdf", "type": "dir"}]


def test_parse_gh_json_raises_on_empty() -> None:
    with pytest.raises(mkt.MarketplaceError, match="empty stdout"):
        mkt._parse_gh_json("   \n\n")


def test_parse_gh_json_raises_on_no_json() -> None:
    with pytest.raises(mkt.MarketplaceError, match="non-JSON"):
        mkt._parse_gh_json("just warnings\nand no JSON\n")


def test_fetch_skill_md_decodes_base64(fake_run: dict[str, Any]) -> None:
    body = "---\nname: pdf\ndescription: PDF tools\n---\n"
    fake_run["next"] = _FakeProc(
        rc=0,
        stdout=json.dumps(
            {
                "encoding": "base64",
                "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                "path": "skills/pdf/SKILL.md",
            }
        ),
    )
    assert mkt.fetch_skill_md("pdf") == body


def test_fetch_skill_md_refuses_suspicious_name() -> None:
    with pytest.raises(mkt.MarketplaceError, match="suspicious"):
        mkt.fetch_skill_md("../etc/passwd")
    with pytest.raises(mkt.MarketplaceError, match="suspicious"):
        mkt.fetch_skill_md(".hidden")


def test_install_tree_url() -> None:
    assert (
        mkt.install_tree_url("skill-creator")
        == "https://github.com/anthropics/skills/tree/main/skills/skill-creator"
    )


def test_gh_missing_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoGh:
        @staticmethod
        def which(name: str) -> str | None:
            return None

    monkeypatch.setattr(mkt, "shutil", _NoGh())
    with pytest.raises(mkt.MarketplaceError, match="gh CLI not found"):
        mkt.list_skills()
