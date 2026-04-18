"""B-2: canonicalize URL drops query + fragment, case-folds scheme/host,
strips `www.`, preserves path case."""

from __future__ import annotations

import importlib

installer_main = importlib.import_module("tools.skill_installer.main")
_canonicalize_url = installer_main._canonicalize_url


def test_canonicalize_url_drops_query_and_fragment() -> None:
    assert (
        _canonicalize_url("https://gh.com/x/y?utm=a#readme")
        == _canonicalize_url("https://gh.com/x/y?utm=b")
        == _canonicalize_url("https://gh.com/x/y/")
        == "https://gh.com/x/y"
    )


def test_canonicalize_url_strips_www() -> None:
    assert _canonicalize_url("https://www.github.com/x/y") == _canonicalize_url(
        "https://github.com/x/y"
    )


def test_canonicalize_url_preserves_path_case() -> None:
    # Anthropics/skills vs anthropics/skills must NOT collapse.
    assert _canonicalize_url("https://github.com/Anthropics/skills") != _canonicalize_url(
        "https://github.com/anthropics/skills"
    )


def test_canonicalize_url_folds_scheme_and_host_case() -> None:
    assert _canonicalize_url("HTTPS://GITHUB.COM/a/b") == "https://github.com/a/b"
