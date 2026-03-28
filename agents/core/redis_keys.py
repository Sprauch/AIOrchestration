"""Shared Redis key helpers for active-work tracking.

These functions are used by base_agent, orchestrator, and web modules
to manage the unresolved-work sets in Redis.
"""

from __future__ import annotations

import time
from datetime import datetime


def _active_keys(stage: str) -> tuple[str, str, str]:
    base = f"orchestrator:active:{stage}"
    return base, f"{base}:msg", f"{base}:ts"


def _claim_keys(stage: str) -> tuple[str, str]:
    base = f"orchestrator:claimed:{stage}"
    return base, f"{base}:ts"


async def _clear_active_work_thread(redis, thread_id: str) -> None:
    """Remove a thread from all unresolved-work structures."""
    if not redis:
        return
    try:
        for stage in ("proposals", "tasks", "reviews"):
            set_key, msg_key, ts_key = _active_keys(stage)
            await redis.srem(set_key, thread_id)
            await redis.hdel(msg_key, thread_id)
            await redis.hdel(ts_key, thread_id)
            claim_key, claim_ts = _claim_keys(stage)
            await redis.hdel(claim_key, thread_id)
            await redis.hdel(claim_ts, thread_id)
    except Exception:
        pass


def _timestamp_to_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


async def _active_work_count(redis, stage: str) -> int:
    """Return unresolved work count for a workflow stage."""
    if not redis:
        return 0
    try:
        active = int(await redis.scard(f"orchestrator:active:{stage}"))
        if stage == "proposals":
            claim_key, _claim_ts = _claim_keys(stage)
            claimed = int(await redis.hlen(claim_key))
            return max(0, active - claimed)
        return active
    except Exception:
        return 0
