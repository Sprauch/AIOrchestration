"""Tests for the thread state module — authoritative thread records and transitions."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from unittest.mock import AsyncMock

import pytest

from agents.core.thread_state import (
    Transition,
    ThreadStatus,
    PipelineStage,
    thread_key,
    index_key,
    create_thread,
    get_thread,
    apply_transition,
    count_queued,
    count_claimed,
    count_active,
    recover_claimed_threads,
)


# ── Fake Redis ──────────────────────────────────────────────

class FakeRedis:
    """Minimal Redis fake supporting hashes, sets, and pipelines."""

    def __init__(self):
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}

    async def hset(self, key, mapping=None, **kwargs):
        self._hashes.setdefault(key, {})
        if mapping:
            self._hashes[key].update({str(k): str(v) for k, v in mapping.items()})

    async def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    async def hget(self, key, field):
        return self._hashes.get(key, {}).get(field)

    async def scard(self, key):
        return len(self._sets.get(key, set()))

    async def sadd(self, key, *members):
        self._sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        s = self._sets.get(key, set())
        for m in members:
            s.discard(m)

    async def smembers(self, key):
        return self._sets.get(key, set()).copy()

    async def delete(self, key):
        self._hashes.pop(key, None)
        self._sets.pop(key, None)

    async def scan_iter(self, pattern):
        prefix = pattern.replace("*", "")
        for key in list(self._hashes.keys()):
            if key.startswith(prefix):
                yield key

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._ops: list = []

    def hset(self, key, mapping=None, **kwargs):
        self._ops.append(("hset", key, mapping))
        return self

    def srem(self, key, *members):
        self._ops.append(("srem", key, members))
        return self

    def sadd(self, key, *members):
        self._ops.append(("sadd", key, members))
        return self

    def delete(self, key):
        self._ops.append(("delete", key))
        return self

    async def execute(self):
        for op in self._ops:
            if op[0] == "hset":
                await self._redis.hset(op[1], mapping=op[2])
            elif op[0] == "srem":
                await self._redis.srem(op[1], *op[2])
            elif op[0] == "sadd":
                await self._redis.sadd(op[1], *op[2])
            elif op[0] == "delete":
                await self._redis.delete(op[1])
        self._ops.clear()


# ── Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_thread():
    r = FakeRedis()
    record = await create_thread(r, "t1", label="Fix bug")
    assert record["thread_id"] == "t1"
    assert record["status"] == "created"
    assert record["stage"] == "proposals"
    assert record["label"] == "Fix bug"

    stored = await get_thread(r, "t1")
    assert stored["thread_id"] == "t1"


@pytest.mark.asyncio
async def test_proposal_queued_auto_creates():
    r = FakeRedis()
    result = await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED, label="New feature")
    assert result is not None
    assert result["status"] == "queued"
    assert result["stage"] == "proposals"
    assert result["label"] == "New feature"
    assert await count_queued(r, "proposals") == 1


@pytest.mark.asyncio
async def test_proposal_claimed():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED, label="Test")
    result = await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED, claimed_by="arch-1")
    assert result is not None
    assert result["status"] == "claimed"
    assert result["claimed_by"] == "arch-1"
    assert await count_queued(r, "proposals") == 0
    assert await count_claimed(r, "proposals") == 1


@pytest.mark.asyncio
async def test_proposal_approved_moves_to_tasks():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED, claimed_by="arch-1")
    result = await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_APPROVE)
    assert result is not None
    assert result["status"] == "queued"
    assert result["stage"] == "tasks"
    assert await count_queued(r, "tasks") == 1
    assert await count_claimed(r, "proposals") == 0


@pytest.mark.asyncio
async def test_proposal_rejected_is_terminal():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED)
    result = await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_REJECT)
    assert result["status"] == "completed"
    # Terminal index
    terminal = await r.smembers(index_key("terminal"))
    assert "t1" in terminal


@pytest.mark.asyncio
async def test_full_happy_path():
    """Thread goes: proposal → task → review → completed."""
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED, label="Feature X")
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED, claimed_by="arch-1")
    await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_APPROVE)
    await apply_transition(r, "t1", Transition.TASK_CLAIMED, claimed_by="dev-1")
    await apply_transition(r, "t1", Transition.TASK_COMPLETED)
    await apply_transition(r, "t1", Transition.REVIEW_CLAIMED, claimed_by="rev-1")
    result = await apply_transition(r, "t1", Transition.REVIEW_APPROVED)

    assert result["status"] == "completed"
    assert await count_active(r, "proposals") == 0
    assert await count_active(r, "tasks") == 0
    assert await count_active(r, "reviews") == 0


@pytest.mark.asyncio
async def test_review_changes_requested_rework_cycle():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED)
    await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_APPROVE)
    await apply_transition(r, "t1", Transition.TASK_CLAIMED, claimed_by="dev-1")
    await apply_transition(r, "t1", Transition.TASK_COMPLETED)
    await apply_transition(r, "t1", Transition.REVIEW_CLAIMED, claimed_by="rev-1")
    result = await apply_transition(r, "t1", Transition.REVIEW_CHANGES_REQUESTED, review_cycles=1)

    assert result["status"] == "queued"
    assert result["stage"] == "tasks"
    assert result["review_cycles"] == "1"
    assert await count_queued(r, "tasks") == 1


@pytest.mark.asyncio
async def test_cycle_blocked():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED)
    await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_APPROVE)
    await apply_transition(r, "t1", Transition.TASK_CLAIMED)
    result = await apply_transition(r, "t1", Transition.CYCLE_BLOCKED, blocked_reason="Max cycles")

    assert result["status"] == "blocked"
    blocked = await r.smembers(index_key("blocked"))
    assert "t1" in blocked


@pytest.mark.asyncio
async def test_abandoned():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    result = await apply_transition(r, "t1", Transition.ABANDONED, terminal_reason="Operator skipped")

    assert result["status"] == "abandoned"
    assert result["terminal_reason"] == "Operator skipped"


@pytest.mark.asyncio
async def test_invalid_transition_rejected():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    # Can't go directly to REVIEW_APPROVED from queued proposals
    result = await apply_transition(r, "t1", Transition.REVIEW_APPROVED)
    assert result is None


@pytest.mark.asyncio
async def test_transition_on_nonexistent_thread_rejected():
    r = FakeRedis()
    result = await apply_transition(r, "t1", Transition.TASK_CLAIMED)
    assert result is None


@pytest.mark.asyncio
async def test_recover_claimed_threads():
    r = FakeRedis()
    # Simulate: thread was claimed but agent died
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED, claimed_by="arch-1")
    assert await count_claimed(r, "proposals") == 1

    requeued = await recover_claimed_threads(r)
    assert requeued == 1
    assert await count_queued(r, "proposals") == 1
    assert await count_claimed(r, "proposals") == 0

    record = await get_thread(r, "t1")
    assert record["status"] == "queued"
    assert record["claimed_by"] == ""


@pytest.mark.asyncio
async def test_pr_merged_on_completed_thread():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED)
    await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_APPROVE)
    await apply_transition(r, "t1", Transition.TASK_CLAIMED)
    await apply_transition(r, "t1", Transition.TASK_COMPLETED)
    await apply_transition(r, "t1", Transition.REVIEW_CLAIMED)
    await apply_transition(r, "t1", Transition.REVIEW_APPROVED)

    result = await apply_transition(r, "t1", Transition.PR_MERGED, pr_status="https://github.com/pr/1")
    assert result is not None
    assert result["pr_status"] == "https://github.com/pr/1"


@pytest.mark.asyncio
async def test_branch_name_persists():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED)
    await apply_transition(r, "t1", Transition.PROPOSAL_REVIEWED_APPROVE)
    result = await apply_transition(r, "t1", Transition.TASK_CLAIMED, branch_name="agent/fix-bug")
    assert result["branch_name"] == "agent/fix-bug"

    # Branch persists through further transitions
    await apply_transition(r, "t1", Transition.TASK_COMPLETED)
    record = await get_thread(r, "t1")
    assert record["branch_name"] == "agent/fix-bug"


@pytest.mark.asyncio
async def test_count_active():
    r = FakeRedis()
    await apply_transition(r, "t1", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t2", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t3", Transition.PROPOSAL_QUEUED)
    await apply_transition(r, "t1", Transition.PROPOSAL_CLAIMED)

    assert await count_active(r, "proposals") == 3  # 2 queued + 1 claimed
    assert await count_queued(r, "proposals") == 2
    assert await count_claimed(r, "proposals") == 1
