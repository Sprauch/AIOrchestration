"""Tests for LRU-bounded dedup cache in AgentProcess._prepare_and_publish_check()."""

import asyncio

import pytest

from agents.core.message import Envelope, MessageType
from agents.roles.developer_agent import DeveloperAgent


class FakeCLI:
    async def send(self, prompt: str) -> str:
        return ""

    async def resume(self):
        pass


class FakeRedis:
    async def set(self, key, value, **kwargs):
        pass

    async def get(self, key):
        return None

    async def xlen(self, key):
        return 0


class FakeBus:
    def __init__(self):
        self.published = []
        self.redis = FakeRedis()

    async def publish(self, channel, envelope):
        self.published.append((channel, envelope))

    async def subscribe_simple(self, channels, last_ids=None):
        return
        yield

    async def get_history(self, channel, count=100):
        return []


def _make_agent(**overrides):
    defaults = dict(
        agent_id="dev-1",
        role="developer",
        cli_session=FakeCLI(),
        bus=FakeBus(),
        safety=None,
    )
    defaults.update(overrides)
    agent = DeveloperAgent(**defaults)
    # Disable thread guard so tests don't need Redis
    agent._thread_guard = None
    return agent


def _make_envelope(thread_id="t1", payload=None):
    return Envelope(
        sender_id="test",
        sender_role="test",
        message_type=MessageType.TASK_PROGRESS,
        payload=payload or {"data": "hello"},
        thread_id=thread_id,
    )


def _make_trace_envelope(thread_id="t1"):
    return Envelope(
        sender_id="test",
        sender_role="test",
        message_type=MessageType.CLI_TRACE,
        payload={"direction": "prompt", "content": "x"},
        thread_id=thread_id,
    )


# ---- Cache size stays bounded ----

@pytest.mark.asyncio
async def test_cache_bounded_at_max_size():
    """Cache never exceeds MAX_DEDUP_CACHE_SIZE entries."""
    agent = _make_agent()
    agent.dedup_cache_size = 100  # small for testing

    for i in range(200):
        env = _make_envelope(payload={"index": i})
        await agent._prepare_and_publish_check(env)

    assert len(agent._publish_hashes) == 100


# ---- Dedup blocks second+ identical publish ----

@pytest.mark.asyncio
async def test_dedup_blocks_second_identical():
    """Second and subsequent identical messages are suppressed (max_repeats=1)."""
    agent = _make_agent()
    env = _make_envelope()

    assert await agent._prepare_and_publish_check(env) is True   # 1st
    assert await agent._prepare_and_publish_check(env) is False  # 2nd blocked
    assert await agent._prepare_and_publish_check(env) is False  # 3rd blocked


# ---- Lookup refreshes LRU recency without resetting count ----

@pytest.mark.asyncio
async def test_lookup_refreshes_recency_keeps_count():
    """Accessing a hash moves it to most-recent without resetting its count."""
    agent = _make_agent()
    agent.dedup_cache_size = 5

    target_env = _make_envelope(payload={"data": "target"})

    # Insert target twice (count=2)
    await agent._prepare_and_publish_check(target_env)
    await agent._prepare_and_publish_check(target_env)

    # Fill cache with other unique entries to push target toward eviction
    for i in range(3):
        await agent._prepare_and_publish_check(
            _make_envelope(payload={"filler": i})
        )

    # Cache is now full (5 entries). Target should still be present.
    # Access target again — this should refresh its recency AND count should be 3 (blocked).
    assert await agent._prepare_and_publish_check(target_env) is False  # count=3

    # Now add more fillers to evict the oldest non-target entries
    for i in range(3, 6):
        await agent._prepare_and_publish_check(
            _make_envelope(payload={"filler": i})
        )

    # Target was refreshed so it should still be in cache
    from agents.core.message import payload_hash
    h = payload_hash(target_env.thread_id, target_env.message_type.value, target_env.payload)
    assert h in agent._publish_hashes


# ---- Evicted hash restarts from 0 ----

@pytest.mark.asyncio
async def test_evicted_hash_restarts_count():
    """After LRU eviction, a repeated hash starts fresh (count=1, allowed)."""
    agent = _make_agent()
    agent.dedup_cache_size = 5

    target_env = _make_envelope(payload={"data": "evict-me"})

    # Insert target twice (count=2, still allowed)
    await agent._prepare_and_publish_check(target_env)
    await agent._prepare_and_publish_check(target_env)

    # Fill with enough unique entries to evict target (it's the oldest)
    for i in range(5):
        await agent._prepare_and_publish_check(
            _make_envelope(payload={"other": i})
        )

    # Target should be evicted
    from agents.core.message import payload_hash
    h = payload_hash(target_env.thread_id, target_env.message_type.value, target_env.payload)
    assert h not in agent._publish_hashes

    # Re-publish target — should be allowed (count restarts at 1)
    assert await agent._prepare_and_publish_check(target_env) is True


# ---- CLI_TRACE bypasses dedup entirely ----

@pytest.mark.asyncio
async def test_cli_trace_bypasses_dedup():
    """CLI_TRACE messages skip dedup and don't affect the cache."""
    agent = _make_agent()
    trace = _make_trace_envelope()

    for _ in range(10):
        assert await agent._prepare_and_publish_check(trace) is True

    # Cache should be empty — traces don't get hashed
    assert len(agent._publish_hashes) == 0


# ---- Eviction order is LRU (least-recently-accessed first) ----

@pytest.mark.asyncio
async def test_eviction_order_is_lru():
    """Least-recently-accessed entry is evicted first, not oldest-inserted."""
    agent = _make_agent()
    agent.dedup_cache_size = 3

    env_a = _make_envelope(payload={"key": "a"})
    env_b = _make_envelope(payload={"key": "b"})
    env_c = _make_envelope(payload={"key": "c"})

    # Insert A, B, C
    await agent._prepare_and_publish_check(env_a)
    await agent._prepare_and_publish_check(env_b)
    await agent._prepare_and_publish_check(env_c)

    # Access A again — refreshes it to most-recent
    await agent._prepare_and_publish_check(env_a)

    # Insert D — should evict B (the least-recently-accessed)
    env_d = _make_envelope(payload={"key": "d"})
    await agent._prepare_and_publish_check(env_d)

    from agents.core.message import payload_hash
    h_a = payload_hash(env_a.thread_id, env_a.message_type.value, env_a.payload)
    h_b = payload_hash(env_b.thread_id, env_b.message_type.value, env_b.payload)

    assert h_a in agent._publish_hashes, "A should still be cached (was refreshed)"
    assert h_b not in agent._publish_hashes, "B should be evicted (least recently accessed)"
