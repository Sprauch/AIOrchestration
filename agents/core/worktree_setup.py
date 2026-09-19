"""Make a fresh worktree usable.

WHY THIS EXISTS. `git worktree add` gives you the tracked files and nothing else. Any
real project keeps things out of git that the build still needs - dependencies, generated
data, large vendor inputs - so a developer agent in a fresh worktree can often edit and
typecheck but cannot build or run what it is changing. It then produces work it could not
have verified, which is worse than producing nothing.

Measured in the project this fork was built for: `npm ci` takes 14 s, but the generated
data directory is gitignored and listed in the build's asset globs, so a build fails
without it. The vendor input it is derived from is 629 MB and also gitignored.

WHAT IS DELIBERATELY NOT HERE: the project's paths. They belong in config.yaml, because
this mechanism has to work for any repository. See `system.worktree_setup`.

COPY, NOT LINK. A junction or symlink is instant and free, and it also means one agent
writing to generated data corrupts it for every other agent at once. A copy confines the
mistake to one worktree. The cost is staleness, which `version_file` below exists to
detect.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from agents.core.worktree import extended_path

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT = 900


class WorktreeSetupError(RuntimeError):
    """Raised when a worktree cannot be made usable. Never swallowed."""


def _copy(src: Path, dst: Path) -> int:
    """Copy a file or directory tree, returning bytes copied. Overwrites."""
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(extended_path(dst), ignore_errors=True)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
        return sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    shutil.copy2(src, dst)
    return dst.stat().st_size


def is_stale(base_dir: Path, worktree_dir: Path, version_file: str | None) -> bool:
    """Has the main checkout's generated data moved on since this worktree was set up?

    Checked when a worktree is REUSED, not when it is created - at creation the copy is
    current by construction. A regeneration in the main checkout is what makes an existing
    worktree wrong, and silently building against last week's data is exactly the failure
    a copy trades for isolation.
    """
    if not version_file:
        return False
    main = Path(base_dir) / version_file
    mine = Path(worktree_dir) / version_file
    if not main.exists():
        return False
    if not mine.exists():
        return True
    try:
        return main.read_bytes() != mine.read_bytes()
    except OSError:
        return True


def setup_worktree(base_dir: str | Path, worktree_dir: str | Path, cfg) -> None:
    """Copy what the build needs, then run the setup commands.

    Raises WorktreeSetupError on any failure. That is deliberate: a worktree that is
    half-ready produces unverifiable work, and the caller must not treat it as usable.
    """
    if cfg is None:
        return
    base = Path(base_dir).resolve()
    work = Path(worktree_dir).resolve()

    for rel in getattr(cfg, "copy_paths", None) or []:
        src = base / rel
        if not src.exists():
            raise WorktreeSetupError(
                f"worktree_setup.copy_paths names '{rel}', which does not exist in {base}. "
                f"Either the path is wrong or the main checkout is itself incomplete."
            )
        started = time.monotonic()
        size = _copy(src, work / rel)
        logger.info(
            "worktree setup: copied %s (%.1f MB in %.1fs)",
            rel, size / 1048576, time.monotonic() - started,
        )

    for command in getattr(cfg, "commands", None) or []:
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=str(work), shell=True, capture_output=True, text=True,
            timeout=getattr(cfg, "command_timeout", None) or DEFAULT_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-600:]
            raise WorktreeSetupError(
                f"worktree_setup command failed in {work}: {command}\n{tail}"
            )
        logger.info(
            "worktree setup: ran %r (%.1fs)", command, time.monotonic() - started,
        )
