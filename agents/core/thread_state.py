"""Thread State — authoritative per-thread state records and atomic transitions.

This module replaces the scattered active-work sets, claim keys, branch mappings,
and cycle tracking with a single authoritative thread record per thread.

Key design:
  - Each thread has one Redis hash: orchestrator:thread:{thread_id}
  - Scheduling indexes (sets) enable fast queries by stage/status
  - All mutations go through apply_transition() for atomicity
  - Streams remain the event log; thread records are the runtime truth
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ── Key Naming ──────────────────────────────────────────────

THREAD_KEY_PREFIX = "orchestrator:thread"
INDEX_PREFIX = "orchestrator:threads"

def thread_key(thread_id: str) -> str:
    return f"{THREAD_KEY_PREFIX}:{thread_id}"

def index_key(status: str, stage: str | None = None) -> str:
    if stage:
        return f"{INDEX_PREFIX}:{status}:{stage}"
    return f"{INDEX_PREFIX}:{status}"


# ── Thread States ───────────────────────────────────────────

class ThreadStatus(str, Enum):
    """Scheduling state — where the thread is in terms of work disposition."""
    CREATED = "created"
    QUEUED = "queued"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        return self in (ThreadStatus.BLOCKED, ThreadStatus.ABANDONED, ThreadStatus.COMPLETED)


class PipelineStage(str, Enum):
    """Where in the pipeline — which role is responsible."""
    PROPOSALS = "proposals"
    TASKS = "tasks"
    REVIEWS = "reviews"


# ── Transitions ─────────────────────────────────────────────

class Transition(str, Enum):
    """Named workflow transitions. Each one maps to a state change."""
    PROPOSAL_QUEUED = "proposal_queued"
    PROPOSAL_CLAIMED = "proposal_claimed"
    PROPOSAL_REVIEWED_APPROVE = "proposal_reviewed_approve"
    PROPOSAL_REVIEWED_REJECT = "proposal_reviewed_reject"
    PROPOSAL_REVIEWED_REVISE = "proposal_reviewed_revise"
    TASK_QUEUED = "task_queued"
    TASK_CLAIMED = "task_claimed"
    TASK_COMPLETED = "task_completed"
    REVIEW_QUEUED = "review_queued"
    REVIEW_CLAIMED = "review_claimed"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    REWORK_QUEUED = "rework_queued"
    CYCLE_BLOCKED = "cycle_blocked"
    ABANDONED = "abandoned"
    PR_CREATED = "pr_created"
    PR_MERGED = "pr_merged"
    PR_SKIPPED = "pr_skipped"
    PR_CLOSED = "pr_closed"


# Transition rules: (transition) → (valid_from_statuses, new_status, new_stage_or_None)
_TRANSITION_RULES: dict[Transition, tuple[set[str], ThreadStatus, PipelineStage | None]] = {
    Transition.PROPOSAL_QUEUED:            ({"created", "queued"},                     ThreadStatus.QUEUED,     PipelineStage.PROPOSALS),
    Transition.PROPOSAL_CLAIMED:           ({"queued"},                                ThreadStatus.CLAIMED,    PipelineStage.PROPOSALS),
    Transition.PROPOSAL_REVIEWED_APPROVE:  ({"claimed", "processing"},                 ThreadStatus.QUEUED,     PipelineStage.TASKS),
    Transition.PROPOSAL_REVIEWED_REJECT:   ({"claimed", "processing"},                 ThreadStatus.COMPLETED,  None),
    Transition.PROPOSAL_REVIEWED_REVISE:   ({"claimed", "processing"},                 ThreadStatus.QUEUED,     PipelineStage.PROPOSALS),
    Transition.TASK_QUEUED:                ({"queued", "claimed"},                      ThreadStatus.QUEUED,     PipelineStage.TASKS),
    Transition.TASK_CLAIMED:               ({"queued"},                                ThreadStatus.CLAIMED,    PipelineStage.TASKS),
    Transition.TASK_COMPLETED:             ({"claimed", "processing"},                 ThreadStatus.QUEUED,     PipelineStage.REVIEWS),
    Transition.REVIEW_QUEUED:              ({"queued", "claimed"},                      ThreadStatus.QUEUED,     PipelineStage.REVIEWS),
    Transition.REVIEW_CLAIMED:             ({"queued"},                                ThreadStatus.CLAIMED,    PipelineStage.REVIEWS),
    Transition.REVIEW_APPROVED:            ({"claimed", "processing"},                 ThreadStatus.COMPLETED,  None),
    Transition.REVIEW_CHANGES_REQUESTED:   ({"claimed", "processing"},                 ThreadStatus.QUEUED,     PipelineStage.TASKS),
    Transition.REWORK_QUEUED:              ({"queued", "claimed", "processing"},       ThreadStatus.QUEUED,     PipelineStage.TASKS),
    Transition.CYCLE_BLOCKED:              ({"queued", "claimed", "processing"},       ThreadStatus.BLOCKED,    None),
    Transition.ABANDONED:                  ({"created", "queued", "claimed", "processing", "blocked"}, ThreadStatus.ABANDONED, None),
    Transition.PR_CREATED:                 ({"completed"},                             ThreadStatus.COMPLETED,  None),
    Transition.PR_MERGED:                  ({"completed"},                             ThreadStatus.COMPLETED,  None),
    Transition.PR_SKIPPED:                 ({"completed", "blocked", "abandoned"},     ThreadStatus.COMPLETED,  None),
    Transition.PR_CLOSED:                  ({"completed"},                             ThreadStatus.COMPLETED,  None),
}


# ── Thread Record Operations ────────────────────────────────

async def get_thread(redis, thread_id: str) -> dict[str, str] | None:
    """Read the full thread record. Returns None if thread doesn't exist."""
    data = await redis.hgetall(thread_key(thread_id))
    return dict(data) if data else None


async def create_thread(redis, thread_id: str, label: str = "", stage: str = "proposals") -> dict[str, str]:
    """Create a new thread record in CREATED state."""
    now = str(time.time())
    record = {
        "thread_id": thread_id,
        "status": ThreadStatus.CREATED.value,
        "stage": stage,
        "label": label,
        "claimed_by": "",
        "revision": "1",
        "review_cycles": "0",
        "branch_name": "",
        "pr_status": "",
        "blocked_reason": "",
        "terminal_reason": "",
        "created_at": now,
        "updated_at": now,
    }
    await redis.hset(thread_key(thread_id), mapping=record)
    return record


async def apply_transition(
    redis,
    thread_id: str,
    transition: Transition,
    *,
    label: str = "",
    claimed_by: str = "",
    branch_name: str = "",
    pr_status: str = "",
    blocked_reason: str = "",
    terminal_reason: str = "",
    review_cycles: int | None = None,
    revision: str = "",
) -> dict[str, str] | None:
    """Apply a named transition to a thread. Returns updated record, or None if rejected.

    This is the single gateway for all thread state mutations.
    It validates the current state, updates the record, and maintains indexes.
    """
    rule = _TRANSITION_RULES.get(transition)
    if not rule:
        logger.warning("Unknown transition: %s", transition)
        return None

    valid_from, new_status, new_stage = rule
    key = thread_key(thread_id)
    now = str(time.time())

    # Read current state
    current = await redis.hgetall(key)
    if not current:
        # Auto-create if this is a queued transition and thread doesn't exist
        if transition == Transition.PROPOSAL_QUEUED:
            current = await create_thread(redis, thread_id, label=label)
        else:
            logger.debug("Transition %s rejected: thread %s not found", transition.value, thread_id[:8])
            return None

    current_status = current.get("status", "created")
    current_stage = current.get("stage", "")

    # Validate transition
    if current_status not in valid_from:
        logger.debug(
            "Transition %s rejected for thread %s: status is %s, expected one of %s",
            transition.value, thread_id[:8], current_status, valid_from,
        )
        return None

    # Build updates
    updates: dict[str, str] = {
        "status": new_status.value,
        "updated_at": now,
    }
    if new_stage:
        updates["stage"] = new_stage.value
    if label:
        updates["label"] = label
    if claimed_by:
        updates["claimed_by"] = claimed_by
    elif new_status in (ThreadStatus.QUEUED, ThreadStatus.COMPLETED, ThreadStatus.BLOCKED, ThreadStatus.ABANDONED):
        updates["claimed_by"] = ""
    if branch_name:
        updates["branch_name"] = branch_name
    if pr_status:
        updates["pr_status"] = pr_status
    if blocked_reason:
        updates["blocked_reason"] = blocked_reason
    if terminal_reason:
        updates["terminal_reason"] = terminal_reason
    if review_cycles is not None:
        updates["review_cycles"] = str(review_cycles)
    if revision:
        updates["revision"] = revision

    # Apply atomically: update record + move indexes
    try:
        pipe = redis.pipeline()

        # Update thread record
        pipe.hset(key, mapping=updates)

        # Remove from old index
        if current_status and current_stage:
            old_idx = index_key(current_status, current_stage)
            pipe.srem(old_idx, thread_id)
        # Also remove from terminal/blocked index in case of re-queue
        pipe.srem(index_key("blocked"), thread_id)
        pipe.srem(index_key("terminal"), thread_id)

        # Add to new index
        if new_stage:
            pipe.sadd(index_key(new_status.value, new_stage.value), thread_id)
        if new_status == ThreadStatus.BLOCKED:
            pipe.sadd(index_key("blocked"), thread_id)
        if new_status.is_terminal:
            pipe.sadd(index_key("terminal"), thread_id)

        await pipe.execute()
    except Exception:
        logger.exception("Failed to apply transition %s for thread %s", transition.value, thread_id[:8])
        return None

    # Merge updates into current for return
    current.update(updates)
    return current


# ── Query Helpers ───────────────────────────────────────────

async def count_queued(redis, stage: str) -> int:
    """Count threads queued at a pipeline stage."""
    try:
        return int(await redis.scard(index_key("queued", stage)))
    except Exception:
        return 0


async def count_claimed(redis, stage: str) -> int:
    """Count threads claimed (in progress) at a pipeline stage."""
    try:
        return int(await redis.scard(index_key("claimed", stage)))
    except Exception:
        return 0


async def count_active(redis, stage: str) -> int:
    """Count all active (queued + claimed) threads at a stage."""
    return await count_queued(redis, stage) + await count_claimed(redis, stage)


async def get_queued_threads(redis, stage: str) -> list[str]:
    """Return thread IDs queued at a stage."""
    try:
        return list(await redis.smembers(index_key("queued", stage)))
    except Exception:
        return []


async def get_blocked_threads(redis) -> list[str]:
    """Return all blocked thread IDs."""
    try:
        return list(await redis.smembers(index_key("blocked")))
    except Exception:
        return []


async def get_terminal_threads(redis) -> list[str]:
    """Return all terminal thread IDs."""
    try:
        return list(await redis.smembers(index_key("terminal")))
    except Exception:
        return []


async def get_all_thread_ids(redis) -> list[str]:
    """Scan for all thread records."""
    thread_ids = []
    try:
        async for key in redis.scan_iter(f"{THREAD_KEY_PREFIX}:*"):
            tid = key.split(":", 2)[-1] if isinstance(key, str) else key.decode().split(":", 2)[-1]
            if tid:
                thread_ids.append(tid)
    except Exception:
        pass
    return thread_ids


# ── Recovery ────────────────────────────────────────────────

async def recover_claimed_threads(redis) -> int:
    """On startup, re-queue any threads that were claimed by dead agents."""
    requeued = 0
    for stage in PipelineStage:
        claimed = await redis.smembers(index_key("claimed", stage.value))
        for tid in (claimed or []):
            record = await get_thread(redis, tid)
            if not record:
                continue
            # Re-queue: move from claimed → queued
            try:
                pipe = redis.pipeline()
                pipe.hset(thread_key(tid), mapping={
                    "status": ThreadStatus.QUEUED.value,
                    "claimed_by": "",
                    "updated_at": str(time.time()),
                })
                pipe.srem(index_key("claimed", stage.value), tid)
                pipe.sadd(index_key("queued", stage.value), tid)
                await pipe.execute()
                requeued += 1
            except Exception:
                logger.debug("Failed to re-queue claimed thread %s", tid[:8], exc_info=True)
    if requeued:
        logger.info("Recovery: re-queued %d claimed threads from previous run", requeued)
    return requeued


async def cleanup_terminal_threads(redis, max_age_seconds: int = 604800) -> int:
    """Remove thread records older than max_age for terminal threads (default 7 days)."""
    removed = 0
    now = time.time()
    terminal = await get_terminal_threads(redis)
    for tid in terminal:
        record = await get_thread(redis, tid)
        if not record:
            continue
        updated = float(record.get("updated_at", 0))
        if now - updated < max_age_seconds:
            continue
        try:
            pipe = redis.pipeline()
            pipe.delete(thread_key(tid))
            pipe.srem(index_key("terminal"), tid)
            pipe.srem(index_key("completed", record.get("stage", "")), tid)
            pipe.srem(index_key("blocked"), tid)
            pipe.srem(index_key("abandoned", record.get("stage", "")), tid)
            await pipe.execute()
            removed += 1
        except Exception:
            pass
    if removed:
        logger.info("Cleanup: removed %d terminal thread records older than %ds", removed, max_age_seconds)
    return removed
