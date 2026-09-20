"""Who proposes the work: the PM agent, or the human.

TWO MODES, AND THE DIFFERENCE IS ONLY THE FIRST STAGE.

  AUTOMATIC  The PM agent reads the codebase and proposes work to the architect. Useful
             when there is no obvious next step and the point is to be shown something
             you would not have thought of.

  MANUAL     You write the proposals. The PM agent does not run. Everything downstream is
             unchanged, because the architect subscribes to a stream of proposals and has
             never cared who wrote them.

Nothing else in the pipeline is mode-aware, deliberately. A human proposal is an ordinary
proposal envelope with sender_role "human"; it routes by target_area exactly as the PM's
do, so a user-facing one still reaches the product designer first.

STORED IN REDIS, NOT IN CONFIG, because the switch has to work while the orchestrator is
running - the whole point is handing over the reins when you run out of next steps, and a
restart to do that would mean losing the work in flight. config.system.mode is the value
used the first time, when Redis has nothing to say.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MODE_KEY = "orchestrator:mode"

AUTOMATIC = "automatic"
MANUAL = "manual"
MODES = (AUTOMATIC, MANUAL)


def normalize(value: str | None, default: str = AUTOMATIC) -> str:
    """Coerce anything to a known mode. Unknown values fall back rather than raise."""
    if not value:
        return default
    v = str(value).strip().lower()
    return v if v in MODES else default


async def get_mode(redis, default: str = AUTOMATIC) -> str:
    """The mode in force right now. Falls back to `default` if Redis cannot answer.

    A read failure must not silently stop the PM, so the fallback is the caller's
    configured default rather than MANUAL.
    """
    if redis is None:
        return default
    try:
        return normalize(await redis.get(MODE_KEY), default)
    except Exception:
        return default


async def set_mode(redis, mode: str) -> str:
    """Record the mode. Returns the normalized value actually stored."""
    value = normalize(mode)
    await redis.set(MODE_KEY, value)
    logger.info("Orchestration mode set to %s", value)
    return value


async def ensure_mode(redis, default: str = AUTOMATIC) -> str:
    """Seed the mode from config the first time, without overwriting a live choice."""
    if redis is None:
        return default
    try:
        existing = await redis.get(MODE_KEY)
        if existing:
            return normalize(existing, default)
        return await set_mode(redis, default)
    except Exception:
        return default
