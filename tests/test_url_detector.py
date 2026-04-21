"""URL detector (phase 3) — B9 trailing-punctuation stripping."""

from __future__ import annotations

from assistant.handlers.message import _detect_urls


def test_detect_plain_https() -> None:
    urls = _detect_urls("https://github.com/foo/bar")
    assert urls == ["https://github.com/foo/bar"]


def test_detect_http() -> None:
    urls = _detect_urls("http://example.com/x")
    assert urls == ["http://example.com/x"]


def test_detect_git_ssh() -> None:
    urls = _detect_urls("clone git@github.com:owner/repo.git please")
    assert urls == ["git@github.com:owner/repo.git"]


def test_detect_mid_sentence() -> None:
    urls = _detect_urls("see https://github.com/foo/bar more")
    assert urls == ["https://github.com/foo/bar"]


def test_detect_multiple() -> None:
    urls = _detect_urls("look at https://a.com/x and https://b.com/y and https://c.com/z")
    assert len(urls) == 3


def test_detect_none() -> None:
    assert _detect_urls("no urls here") == []


def test_detect_markdown_link() -> None:
    urls = _detect_urls("see [this](https://github.com/foo/bar)")
    # The trailing ``)`` gets stripped per B9.
    assert urls == ["https://github.com/foo/bar"]


def test_detect_with_query_string() -> None:
    urls = _detect_urls("https://github.com/foo/bar?ref=main&x=1")
    assert urls == ["https://github.com/foo/bar?ref=main&x=1"]


def test_trailing_punctuation_stripped() -> None:
    """B9: ``https://foo.com.`` → ``https://foo.com`` (strip trailing dot)."""
    assert _detect_urls("see https://github.com/foo/bar.") == ["https://github.com/foo/bar"]
    assert _detect_urls("and https://github.com/foo/bar, more") == ["https://github.com/foo/bar"]
    assert _detect_urls("amazing: https://github.com/foo/bar!") == ["https://github.com/foo/bar"]
