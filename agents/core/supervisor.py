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

    # -u: unbuffered. Python block-buffers stdout when it is a file rather than a
    # terminal, so a crash or a kill loses whatever had not been flushed - which is
    # exactly what happened the first time this was used: the log ended mid-run and the
    # one recorded error had no traceback anywhere. A log that is only complete when the
    # process exits cleanly is no use for diagnosing the times it does not.
    cmd = [sys.executable, "-u", "-m", "agents", "run", "--config", config_path]

    # Send the child's output to a FILE rather than inheriting or piping.
    #
    # Inheriting is useless to a detached process with no console - which is how this
    # failed the first time: the orchestrator crashed during startup and its traceback
    # went nowhere, so the dashboard could only report that the process had gone. Piping
    # is worse: nothing reads the pipe, and a full buffer would block the orchestrator
    # once it had logged enough. A file keeps the evidence and cannot block.
    log_dir = root / "agents" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "orchestrator-spawn.log"
    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")

    kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
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
        log_handle.close()
        raise SupervisorError(f"could not start orchestrator: {exc}") from exc
    finally:
        # The child holds its own handle; this one would otherwise keep the file open
        # in the web server for as long as it runs.
        log_handle.close()

    # CONFIRM IT SURVIVED. Popen succeeding only means the process was created, and
    # reporting that as "started" is how a crash-on-startup reached the UI as an
    # unexplained failure. Startup does preflight and connects Redis before it can fail,
    # so a couple of seconds covers the crash-immediately case without making the request
    # wait for a healthy start - the caller polls the heartbeat for that.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        time.sleep(0.25)
        code = proc.poll()
        if code is not None:
            raise SupervisorError(
                f"orchestrator exited immediately (code {code}). Last output:\n"
                f"{_log_tail(log_path)}"
            )

    logger.info("Spawned orchestrator pid %s from %s (log: %s)", proc.pid, root, log_path)
    return proc.pid


def _log_tail(path: Path, lines: int = 12) -> str:
    """The last few lines of the spawn log, for an error message a person can act on."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "(no output captured)"
    if not text:
        return "(no output captured)"
    return "\n".join(text[-lines:])
