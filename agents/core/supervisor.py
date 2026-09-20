"""Start and stop the orchestrator from outside it.

WHY THIS EXISTS. The dashboard could show that the orchestrator was disconnected and do
nothing about it, which meant going back to a terminal to run a script - the exact
friction the dashboard exists to remove, and worse when the dashboard is being read from
a phone.

WHAT IT DELIBERATELY DOES NOT DO. It does not run agents in the web server's own process.
The orchestrator is long-lived, manages child CLI sessions and traps signals; hosting it
inside an aiohttp request would tie its lifetime to a browser tab and make a dashboard
restart kill the work. It spawns a detached child instead, exactly as a person typing
the command would, so the orchestrator outlives the dashboard and is stopped on purpose
rather than by accident.

SINGLE INSTANCE, ENFORCED BY REDIS RATHER THAN BY THIS PROCESS. The orchestrator already
publishes a heartbeat, and the dashboard already reads it. That heartbeat is the truth
about whether one is running - a PID file would go stale on a crash, and a process scan
cannot tell one project's orchestrator from another's. Checking the heartbeat means the
answer is the same one the UI is showing.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Seconds after which a heartbeat is considered dead. Matches the dashboard's own
# threshold, so the button and the status line can never disagree.
HEARTBEAT_STALE_AFTER = 30


class SupervisorError(RuntimeError):
    """Raised when the orchestrator cannot be started. Carries a message for the UI."""


async def is_running(redis, stale_after: int = HEARTBEAT_STALE_AFTER) -> bool:
    """Is an orchestrator alive, according to the heartbeat the dashboard already reads?"""
    try:
        hb = await redis.get("orchestrator:heartbeat")
    except Exception:
        return False
    if not hb:
        return False
    try:
        return (time.time() - float(hb)) <= stale_after
    except (TypeError, ValueError):
        return False


def _repo_root() -> Path:
    """The directory containing the `agents` package, which is where `-m agents` runs."""
    return Path(__file__).resolve().parents[2]


def spawn_orchestrator(config_path: str = "agents/config.yaml") -> int:
    """Start `python -m agents run` as a detached child. Returns its pid.

    Detached on purpose: the child must not die when the dashboard restarts, and must not
    inherit the web server's console on Windows, where Ctrl+C in one window would
    otherwise reach both.
    """
    root = _repo_root()
    cfg = root / config_path
    if not cfg.exists():
        raise SupervisorError(f"config not found: {cfg}")

    cmd = [sys.executable, "-m", "agents", "run", "--config", config_path]

    kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        # Inherit stdout/stderr rather than piping: nothing reads a pipe here, and a full
        # pipe buffer would block the orchestrator once it had logged enough.
        "stdout": None,
        "stderr": None,
    }
    if os.name == "nt":
        # New process group + no window: survives the parent, and Ctrl+C in the dashboard
        # window does not propagate into the orchestrator.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        raise SupervisorError(f"could not start orchestrator: {exc}") from exc

    logger.info("Spawned orchestrator pid %s from %s", proc.pid, root)
    return proc.pid
