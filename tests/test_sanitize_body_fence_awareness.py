"""Should-fix #6: `sanitize_body` preserves `---` inside code fences."""

from __future__ import annotations

from tools.memory._lib.frontmatter import sanitize_body


def test_dashes_inside_code_fence_preserved() -> None:
    body = "```\n---\n```\n"
    # Inside a code fence, the `---` is not at risk of spoofing
    # frontmatter — keep it verbatim so docs about YAML survive.
    assert sanitize_body(body) == body


def test_dashes_outside_code_fence_still_indented() -> None:
    body = "prose\n---\nmore\n"
    assert sanitize_body(body) == "prose\n ---\nmore\n"


def test_mixed_in_and_out_of_fence() -> None:
    src = (
        "line1\n"
        "---\n"  # outside a fence — must be indented
        "```\n"
        "---\n"  # inside a fence — preserved
        "```\n"
        "---\n"  # back outside — indented again
    )
    want = "line1\n ---\n```\n---\n```\n ---\n"
    assert sanitize_body(src) == want


def test_unclosed_fence_stays_in_fence() -> None:
    """An opening ``` with no close keeps the sanitiser in fence-mode,
    which is the safer choice — we do NOT touch the `---` inside.
    """
    src = "```\n---\nno close"
    assert sanitize_body(src) == src
