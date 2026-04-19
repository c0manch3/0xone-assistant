"""Phase 7 / commit 11 — hook factory backward-compatibility.

The phase-7 extensions added an optional `data_dir: Path | None = None`
kwarg to `make_pretool_hooks`, `make_bash_hook`, and `make_file_hook`.
The 9 existing phase-3/5/6 test call sites construct these factories
WITHOUT a data_dir. This file locks in the backward-compat contract:

  * factories are callable with positional `project_root` only;
  * the returned Bash hook rejects `tools/render_doc/main.py` invocations
    with the precise operator-facing message
    "render-doc requires data_dir-bound hooks";
  * the other three media CLIs (transcribe / genimage / extract_doc)
    keep working without a data_dir, since they have no `data_dir`-
    bound path guards beyond what the CLI itself enforces.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from assistant.bridge.hooks import (
    check_bash_command,
    make_bash_hook,
    make_file_hook,
    make_pretool_hooks,
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    for sub in ("transcribe", "genimage", "extract_doc", "render_doc"):
        (tmp_path / "tools" / sub).mkdir(parents=True)
        (tmp_path / "tools" / sub / "main.py").write_text("# stub\n")
    return tmp_path


def test_make_bash_hook_callable_without_data_dir(project_root: Path) -> None:
    hook = make_bash_hook(project_root)
    assert callable(hook)


def test_make_file_hook_callable_without_data_dir(project_root: Path) -> None:
    hook = make_file_hook(project_root)
    assert callable(hook)


def test_make_pretool_hooks_callable_without_data_dir(project_root: Path) -> None:
    matchers = make_pretool_hooks(project_root)
    # Bash + 5 file tools + WebFetch = 7 matchers (matches phase-3 contract).
    assert len(matchers) == 7


def test_data_dir_is_keyword_default_none() -> None:
    """Signature check — `data_dir` must default to `None` so existing
    call sites that pass only `project_root` continue to work.
    """
    for fn in (make_bash_hook, make_file_hook, make_pretool_hooks):
        sig = inspect.signature(fn)
        assert "data_dir" in sig.parameters, f"{fn.__name__} missing data_dir param"
        assert sig.parameters["data_dir"].default is None, (
            f"{fn.__name__}.data_dir must default to None; "
            f"got {sig.parameters['data_dir'].default!r}"
        )


# ---------------------------------------------------------------- render_doc hard deny


def test_render_doc_denied_without_data_dir(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/render_doc/main.py --body-file /abs/body.txt --out /abs/doc.pdf",
        project_root,
    )
    assert reason is not None
    assert "render-doc requires data_dir-bound hooks" in reason


def test_render_doc_denied_without_data_dir_even_with_good_paths(
    project_root: Path, tmp_path: Path
) -> None:
    # Even when the arguments LOOK valid, absence of `data_dir` MUST deny.
    d = tmp_path / "data"
    (d / "run" / "render-stage").mkdir(parents=True)
    (d / "media" / "outbox").mkdir(parents=True)
    reason = check_bash_command(
        f"python tools/render_doc/main.py "
        f"--body-file {d / 'run' / 'render-stage' / 'body.txt'} "
        f"--out {d / 'media' / 'outbox' / 'doc.pdf'}",
        project_root,
    )
    assert reason is not None
    assert "render-doc requires data_dir-bound hooks" in reason


# ---------------------------------------------------------------- other tools still work


def test_transcribe_still_works_without_data_dir(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/audio.ogg",
        project_root,
    )
    assert reason is None, reason


def test_genimage_still_works_without_data_dir(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "cat" --out /abs/x.png',
        project_root,
    )
    assert reason is None, reason


def test_extract_doc_still_works_without_data_dir(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/file.pdf",
        project_root,
    )
    assert reason is None, reason


def test_phase3_phase5_phase6_patterns_unchanged(project_root: Path) -> None:
    """Smoke: legacy invocation shapes from earlier phases still pass."""
    (project_root / "tools" / "schedule").mkdir(parents=True, exist_ok=True)
    (project_root / "tools" / "schedule" / "main.py").write_text("# stub\n")
    (project_root / "tools" / "task").mkdir(parents=True, exist_ok=True)
    (project_root / "tools" / "task" / "main.py").write_text("# stub\n")
    cases = [
        "python tools/schedule/main.py list",
        'python tools/task/main.py spawn --kind general --task "hi"',
        "ls tools",
        "pwd",
    ]
    for cmd in cases:
        assert check_bash_command(cmd, project_root) is None, cmd
