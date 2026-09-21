"""Work that was refused at a gate, kept so it can be revisited.

A REFUSAL IS NOT A DELETION. Until now a denied gate dropped the message on the floor:
the architect's task, with everything it had worked out, simply ceased to exist, and the
only way back was to let the whole thread run again from the top and hope it arrived
somewhere similar. That makes saying no expensive, which is the opposite of what a gate
is for — the point of stopping work early is that stopping should be CHEAP.

So a refusal is recorded with the message it refused, and there are two ways back:

  REINSTATE  Approve the same work after all, unchanged. For a refusal made in error, or
             where the reason for refusing has gone away while the project has not moved:
             the task was right, the moment was wrong.

  RESTART    Run it again from the proposal that produced it. For when the project HAS
             moved — the reasoning that produced this task was done against a codebase
             that no longer exists, so reusing its conclusion would be reusing a stale
             premise. This asks for the work to be thought through again.

Which one applies is a judgement about whether the CONTEXT changed, and only a user can
make it. Both are offered; neither is guessed at.

Timeouts are recorded the same way. An expired gate is refused rather than allowed, and
"nobody was looking for a week" is exactly the case where revisiting matters most.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)

REFUSED_KEY = "orchestrator:refused"

# Where a message goes when it is reinstated — the channel its author would have used.
# Keyed by MessageType value so this module needs no import from message.py.
CHANNEL_FOR_TYPE = {
    "proposal": "proposals",
    "design_feedback": "design-feedback",
    "proposal_review": "reviews",
    "task_assignment": "tasks",
    "task_progress": "progress",
    "review_request": "review-requests",
    "review_result": "review-results",
}


async def record(redis, envelope, action: str, reason: str, outcome: str) -> str | None:
    """Keep a refused message so it can be reinstated or restarted. Returns its id."""
    if redis is None:
        return None
    refusal_id = uuid.uuid4().hex[:12]
    entry = {
        "id": refusal_id,
        "action": action,
        "reason": reason,
        "outcome": outcome,  # "denied" or "expired"
        "refused_at": time.time(),
        "thread_id": envelope.thread_id,
        "message_type": envelope.message_type.value,
        "sender_role": envelope.sender_role,
        "payload": envelope.payload,
    }
    try:
        await redis.hset(REFUSED_KEY, refusal_id, json.dumps(entry))
        logger.info(
            "Recorded refusal %s (%s on thread %s) — revisitable",
            refusal_id, action, envelope.thread_id[:8],
        )
        return refusal_id
    except Exception:
        # Never let bookkeeping turn a refusal into an error. The work is already stopped.
        logger.exception("Could not record refusal for thread %s", envelope.thread_id[:8])
        return None


async def list_all(redis) -> list[dict]:
    """Every outstanding refusal, newest first."""
    if redis is None:
        return []
    try:
        raw = await redis.hgetall(REFUSED_KEY)
    except Exception:
        return []
    out = []
    for value in (raw or {}).values():
        try:
            out.append(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            continue
    out.sort(key=lambda e: e.get("refused_at", 0), reverse=True)
    return out


async def get(redis, refusal_id: str) -> dict | None:
    if redis is None:
        return None
    try:
        value = await redis.hget(REFUSED_KEY, refusal_id)
    except Exception:
        return None
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


async def resolve(redis, refusal_id: str) -> None:
    """Drop a refusal once it has been acted on, so it is offered only once."""
    if redis is None:
        return
    try:
        await redis.hdel(REFUSED_KEY, refusal_id)
    except Exception:
        logger.exception("Could not clear refusal %s", refusal_id)
