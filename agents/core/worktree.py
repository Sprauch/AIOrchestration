"""Worktree removal that verifies the worktree is gone.

WHY THIS EXISTS. Both cleanup paths in orchestrator.py did the same two things:

    subprocess.run(["git", "worktree", "remove", "--force", d], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)

Neither call can report failure. `capture_output=True` with no returncode check swallows
git's error, and `ignore_errors=True` swallows the filesystem's. On Windows the first one
fails reliably - `git worktree remove` returns

    error: failed to delete '...': Filename too long

for any worktree containing `node_modules`, whose nesting exceeds MAX_PATH - and the
fallback then gives up silently. The result is a cleanup that always reports success and
sometimes does nothing, which is worse than one that fails loudly: worktrees accumulate
while appearing to be handled.

Reproduced on Windows 11 with a JavaScript project, 2026-09-19.

TWO CLEANUPS, TWO POSTURES. They are not the same operation:

  startup     faces state nobody understands after a crash. The tree is garbage, so
              force=True is correct.
  post-merge  faces a tree whose work is known to be finished. Uncommitted work there is
              a surprise worth surfacing, so force=False and let git refuse.

The shared part is the verification, which both were missing.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def extended_path(p: Path | str) -> str:
    """Return a path form that Windows APIs accept beyond MAX_PATH.

    The `\\\\?\\` prefix disables path parsing and lifts the 260-character limit. It
    requires an absolute, already-normalised path, which is why this resolves first.
    On anything but Windows it is a no-op.
    """
    s = str(Path(p).resolve())
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def _clear_readonly_and_retry(func, path, _exc_info):
    """rmtree error handler: git's object store is read-only, which blocks deletion."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        logger.debug("could not remove %s", path, exc_info=True)


def remove_worktree(base_dir: str | Path, worktree_dir: str | Path, *, force: bool) -> bool:
    """Remove a git worktree and its directory. Returns True only if it is GONE.

    The return value is the point of this function. Callers must not assume success.

    force=True  - for crash recovery, where the tree's contents are not worth preserving.
    force=False - for routine reclaim after a merge; git refuses if the tree is dirty,
                  and that refusal is information rather than an obstacle.
    """
    worktree = Path(worktree_dir)
    if not worktree.exists():
        subprocess.run(["git", "worktree", "prune"], cwd=str(base_dir), capture_output=True)
        return True

    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(worktree))
    result = subprocess.run(cmd, cwd=str(base_dir), capture_output=True, text=True)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if not force and "contains modified or untracked files" in stderr:
            # The refusal we asked for. Do not escalate to force - surface it.
            logger.warning(
                "Worktree %s has uncommitted work and was NOT removed: %s",
                worktree, stderr,
            )
            return False
        logger.info("git worktree remove failed for %s (%s); removing directly", worktree, stderr)

    if worktree.exists():
        # The long-path fallback. shutil.rmtree without `ignore_errors` so a real failure
        # reaches the check below rather than being discarded.
        try:
            shutil.rmtree(extended_path(worktree), onerror=_clear_readonly_and_retry)
        except OSError:
            logger.debug("rmtree raised for %s", worktree, exc_info=True)

    subprocess.run(["git", "worktree", "prune"], cwd=str(base_dir), capture_output=True)

    if worktree.exists():
        logger.error(
            "WORKTREE NOT REMOVED: %s still exists after git and filesystem removal. "
            "It will accumulate. On Windows this is usually a file held open by a running "
            "process, or a path beyond MAX_PATH that the extended prefix did not cover.",
            worktree,
        )
        return False
    return True
