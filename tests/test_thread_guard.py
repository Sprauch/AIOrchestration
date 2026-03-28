"""Tests for per-thread workflow guard."""

import pytest

from agents.core.thread_guard import ThreadGuard


class FakeRedis:
    def __init__(self):
        self._data: dict[str, dict[str, str]] = {}

    async def hget(self, key, field):
        return self._data.get(key, {}).get(field)

    async def hincrby(self, key, field, amount):
        self._data.setdefault(key, {})
        current = int(self._data[key].get(field, 0))
        self._data[key][field] = str(current + amount)

    async def eval(self, script, num_keys, *args):
        """Simulate the ThreadGuard Lua script atomically."""
        key = args[0]   # KEYS[1]
        field = args[1]  # ARGV[1]
        max_rounds = int(args[2])  # ARGV[2]
        incr = int(args[3])  # ARGV[3]
        current = int(self._data.get(key, {}).get(field, 0))
        if current >= max_rounds:
            return -1
        if incr == 1:
            await self.hincrby(key, field, 1)
        return current


class FakeRedisEvalError(FakeRedis):
    """FakeRedis that raises on eval (simulates Redis failure)."""

    async def eval(self, *args, **kwargs):
        raise ConnectionError("simulated eval failure")


@pytest.mark.asyncio
async def test_allows_first_review_cycle():
    guard = ThreadGuard(FakeRedis(), max_rounds=3)
    allowed = await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    assert allowed is True


@pytest.mark.asyncio
async def test_blocks_after_max_failed_rounds():
    guard = ThreadGuard(FakeRedis(), max_rounds=2)
    assert await guard.check_and_increment("t1", "review_result", decision="changes_requested") is True
    assert await guard.check_and_increment("t1", "review_result", decision="changes_requested") is True
    assert await guard.check_and_increment("t1", "review_result", decision="changes_requested") is False


@pytest.mark.asyncio
async def test_approved_does_not_increment():
    guard = ThreadGuard(FakeRedis(), max_rounds=2)
    # Two approvals — should NOT fill the quota
    await guard.check_and_increment("t1", "review_result", decision="approved")
    await guard.check_and_increment("t1", "review_result", decision="approved")
    await guard.check_and_increment("t1", "review_result", decision="approved")
    # Still allowed because approved doesn't count
    assert await guard.check_and_increment("t1", "review_result", decision="changes_requested") is True
    assert await guard.get_cycle_count("t1") == 1


@pytest.mark.asyncio
async def test_review_request_blocked_when_limit_reached():
    guard = ThreadGuard(FakeRedis(), max_rounds=1)
    await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    # Review request should also be blocked
    assert await guard.check_and_increment("t1", "review_request") is False


@pytest.mark.asyncio
async def test_different_threads_independent():
    guard = ThreadGuard(FakeRedis(), max_rounds=1)
    await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    assert await guard.check_and_increment("t1", "review_result", decision="changes_requested") is False
    assert await guard.check_and_increment("t2", "review_result", decision="changes_requested") is True


@pytest.mark.asyncio
async def test_non_review_types_always_allowed():
    guard = ThreadGuard(FakeRedis(), max_rounds=1)
    await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    # Non-review types are not affected
    assert await guard.check_and_increment("t1", "proposal") is True
    assert await guard.check_and_increment("t1", "task_assignment") is True


@pytest.mark.asyncio
async def test_get_cycle_count():
    guard = ThreadGuard(FakeRedis(), max_rounds=5)
    assert await guard.get_cycle_count("t1") == 0
    await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    assert await guard.get_cycle_count("t1") == 1
    await guard.check_and_increment("t1", "review_result", decision="approved")
    assert await guard.get_cycle_count("t1") == 1  # approved doesn't increment
    await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    assert await guard.get_cycle_count("t1") == 2


@pytest.mark.asyncio
async def test_redis_eval_error_returns_false():
    """Fail closed: if Redis eval raises, deny the message."""
    guard = ThreadGuard(FakeRedisEvalError(), max_rounds=3)
    result = await guard.check_and_increment("t1", "review_result", decision="changes_requested")
    assert result is False
