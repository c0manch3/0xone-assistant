from __future__ import annotations

from pathlib import Path

from assistant.logger import get_logger

log = get_logger("bridge.bootstrap")

_SYMLINK_TARGET = Path("../skills")


def ensure_skills_symlink(project_root: Path) -> None:
    """Ensure `<project_root>/.claude/skills` is a symlink to `../skills`.

    Idempotent. Raises RuntimeError if `.claude/skills` exists as a
    non-symlink real directory / file (refuses to clobber user data).
    """
    claude_dir = project_root / ".claude"
    link = claude_dir / "skills"
    claude_dir.mkdir(exist_ok=True)

    if link.is_symlink():
        try:
            current = link.readlink()
        except OSError as exc:
            raise RuntimeError(f"cannot readlink {link}: {exc}") from exc
        if current == _SYMLINK_TARGET:
            log.debug("skills_symlink_ok", link=str(link))
            return
        link.unlink()
    elif link.exists():
        # Tolerate an empty real directory (e.g. leftover from spike scaffolding)
        # but refuse to remove a non-empty one.
        if link.is_dir() and not any(link.iterdir()):
            link.rmdir()
        else:
            raise RuntimeError(f".claude/skills exists and is not a symlink (or not empty): {link}")

    link.symlink_to(_SYMLINK_TARGET, target_is_directory=True)
    log.info("skills_symlink_created", link=str(link), target=str(_SYMLINK_TARGET))
