"""Tests for per-stage WIP limits and review backpressure."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.core.base_agent import AgentProcess
from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus


# ── Helpers ──────────────────────────────────────────────


class StubAgent(AgentProcess):
    """Minimal concrete AgentProcess for backpressure tests."""

    def default_channels(self):
        return {"subscribes_to": ["system"], "publishes_to": ["proposals", "tasks", "review-requests"]}

    def format_prompt(self, envelope):
        return "test"

    def parse_response(self, raw, source_envelope):
        return []


def _make_bus(
    active_map: dict[str, int] | None = None,
    active_members: dict[str, set[str]] | None = None,
) -> MagicMock:
    """Create a mocked MessageBus with configurable active-work counts and membership."""
    bus = MagicMock(spec=MessageBus)
    bus.redis = AsyncMock()
    bus.publish = AsyncMock()

    active = defaultdict(int, active_map or {})
    members = defaultdict(set)
    for key, vals in (active_members or {}).items():
        members[key] = set(vals)
        if key not in active:
            active[key] = len(vals)
    hashes = defaultdict(dict)

    async def fake_scard(key: str) -> int:
        return active.get(key, 0)

    async def fake_sismember(key: str, member: str) -> bool:
        return member in members[key]

    async def fake_sadd(key: str, member: str) -> int:
        before = len(members[key])
        members[key].add(member)
        active[key] = len(members[key])
        return 1 if len(members[key]) > before else 0

    async def fake_srem(key: str, member: str) -> int:
        before = len(members[key])
        members[key].discard(member)
        active[key] = len(members[key])
        return 1 if len(members[key]) < before else 0

    async def fake_hset(key: str, field: str, value: str) -> int:
        hashes[key][field] = value
        return 1

    async def fake_hdel(key: str, field: str) -> int:
        return 1 if hashes[key].pop(field, None) is not None else 0

    async def fake_hexists(key: str, field: str) -> bool:
        return field in hashes[key]

    async def fake_hget(key: str, field: str):
        return hashes[key].get(field)

    bus.redis.scard = AsyncMock(side_effect=fake_scard)
    bus.redis.sismember = AsyncMock(side_effect=fake_sismember)
    bus.redis.sadd = AsyncMock(side_effect=fake_sadd)
    bus.redis.srem = AsyncMock(side_effect=fake_srem)
    bus.redis.hset = AsyncMock(side_effect=fake_hset)
    bus.redis.hdel = AsyncMock(side_effect=fake_hdel)
    bus.redis.hexists = AsyncMock(side_effect=fake_hexists)
    bus.redis.hget = AsyncMock(side_effect=fake_hget)

    async def fake_hlen(key: str) -> int:
        return len(hashes[key])

    bus.redis.hlen = AsyncMock(side_effect=fake_hlen)
    return bus


def _make_agent(role: str, bus: MagicMock, **kwargs) -> StubAgent:
    defaults = dict(
        agent_id=f"{role}-1",
        role=role,
        cli_session=MagicMock(),
        bus=bus,
        max_pending_proposals=3,
        max_pending_tasks=3,
        max_pending_reviews=5,
    )
    defaults.update(kwargs)
    return StubAgent(**defaults)


def _envelope(message_type: MessageType, **payload_overrides) -> Envelope:
    payload = {"title": "Test"}
    payload.update(payload_overrides)
    return Envelope(
        sender_id="test-1",
        sender_role="pm",
        message_type=message_type,
        payload=payload,
    )


# ── _check_stage_gate ────────────────────────────────────


@pytest.mark.asyncio
async def test_pm_gated_when_proposals_full():
    bus = _make_bus({"orchestrator:active:proposals": 3})
    agent = _make_agent("pm", bus)
    assert await agent._check_stage_gate() is True


@pytest.mark.asyncio
async def test_pm_not_gated_when_proposals_below_limit():
    bus = _make_bus({"orchestrator:active:proposals": 2})
    agent = _make_agent("pm", bus)
    assert await agent._check_stage_gate() is False


@pytest.mark.asyncio
async def test_pm_revision_bypasses_gate_for_tracked_thread():
    """Tracked-thread revisions should bypass the PM gate."""
    bus = _make_bus(
        {"orchestrator:active:proposals": 10},
        {"orchestrator:active:proposals": {"thread-123"}},
    )
    agent = _make_agent("pm", bus)
    envelope = Envelope(
        sender_id="architect-1",
        sender_role="architect",
        message_type=MessageType.PROPOSAL_REVIEW,
        payload={"decision": "needs_revision"},
        thread_id="thread-123",
    )
    assert await agent._check_stage_gate(envelope) is False


@pytest.mark.asyncio
async def test_pm_revision_blocked_when_thread_not_tracked():
    """Untracked-thread revisions should still be blocked when backlog is full."""
    bus = _make_bus(
        {"orchestrator:active:proposals": 10},
        {"orchestrator:active:proposals": {"other-thread"}},
    )
    agent = _make_agent("pm", bus)
    envelope = Envelope(
        sender_id="architect-1",
        sender_role="architect",
        message_type=MessageType.PROPOSAL_REVIEW,
        payload={"decision": "needs_revision"},
        thread_id="thread-123",
    )
    assert await agent._check_stage_gate(envelope) is True


@pytest.mark.asyncio
async def test_pm_not_gated_by_reviews_or_tasks():
    """PM is only gated by proposal depth, not tasks or reviews."""
    bus = _make_bus({
        "orchestrator:active:proposals": 0,
        "orchestrator:active:tasks": 100,
        "orchestrator:active:reviews": 100,
    })
    agent = _make_agent("pm", bus)
    assert await agent._check_stage_gate() is False


@pytest.mark.asyncio
async def test_architect_never_gated_at_message_level():
    """Architect is gated at publish time, not at message level."""
    bus = _make_bus({"orchestrator:active:tasks": 100, "orchestrator:active:reviews": 100})
    agent = _make_agent("architect", bus)
    assert await agent._check_stage_gate() is False


@pytest.mark.asyncio
async def test_developer_never_gated():
    bus = _make_bus({"orchestrator:active:reviews": 100})
    agent = _make_agent("developer", bus)
    assert await agent._check_stage_gate() is False


@pytest.mark.asyncio
async def test_reviewer_never_gated():
    bus = _make_bus({"orchestrator:active:reviews": 100})
    agent = _make_agent("reviewer", bus)
    assert await agent._check_stage_gate() is False


# ── Publish-time gates ───────────────────────────────────


@pytest.mark.asyncio
async def test_publish_proposal_blocked_when_full():
    """PROPOSAL publish breaks when proposal queue is at capacity."""
    bus = _make_bus({"orchestrator:active:proposals": 3})
    agent = _make_agent("pm", bus)
    agent.publish_channels = ["proposals"]

    env = _envelope(MessageType.PROPOSAL)
    agent.parse_response = lambda raw, src: [env]
    agent._prepare_and_publish_check = AsyncMock(return_value=True)
    agent._check_output_safety = AsyncMock(return_value=True)
    agent._route_outgoing = lambda e: "proposals"
    agent._log_turn = lambda *a: None
    agent._metrics = None
    agent._publish_trace = AsyncMock()
    agent._check_stage_gate = AsyncMock(return_value=False)
    agent._is_paused = AsyncMock(return_value=False)
    agent.cli.send = AsyncMock(return_value="response")

    incoming = _envelope(MessageType.SYSTEM)
    await agent._handle_message(incoming)

    # Proposal should NOT have been published (queue full at publish time)
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_publish_task_blocked_when_tasks_full():
    """TASK_ASSIGNMENT held when task queue is at capacity."""
    bus = _make_bus({"orchestrator:active:tasks": 3, "orchestrator:active:reviews": 0})
    agent = _make_agent("architect", bus)
    agent.publish_channels = ["tasks"]

    env = _envelope(MessageType.TASK_ASSIGNMENT)
    agent.parse_response = lambda raw, src: [env]
    agent._prepare_and_publish_check = AsyncMock(return_value=True)
    agent._check_output_safety = AsyncMock(return_value=True)
    agent._route_outgoing = lambda e: "tasks"
    agent._log_turn = lambda *a: None
    agent._metrics = None
    agent._publish_trace = AsyncMock()
    agent._check_stage_gate = AsyncMock(return_value=False)
    agent._is_paused = AsyncMock(return_value=False)
    agent.cli.send = AsyncMock(return_value="response")

    incoming = _envelope(MessageType.PROPOSAL)
    await agent._handle_message(incoming)

    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_publish_task_blocked_when_reviews_full():
    """TASK_ASSIGNMENT held when review queue is at capacity (review backpressure)."""
    bus = _make_bus({"orchestrator:active:tasks": 0, "orchestrator:active:reviews": 5})
    agent = _make_agent("architect", bus)
    agent.publish_channels = ["tasks"]

    env = _envelope(MessageType.TASK_ASSIGNMENT)
    agent.parse_response = lambda raw, src: [env]
    agent._prepare_and_publish_check = AsyncMock(return_value=True)
    agent._check_output_safety = AsyncMock(return_value=True)
    agent._route_outgoing = lambda e: "tasks"
    agent._log_turn = lambda *a: None
    agent._metrics = None
    agent._publish_trace = AsyncMock()
    agent._check_stage_gate = AsyncMock(return_value=False)
    agent._is_paused = AsyncMock(return_value=False)
    agent.cli.send = AsyncMock(return_value="response")

    incoming = _envelope(MessageType.PROPOSAL)
    await agent._handle_message(incoming)

    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_developer_always_publishes_review_request():
    """Developer REVIEW_REQUEST is never blocked — review backpressure is upstream only."""
    bus = _make_bus({"orchestrator:active:reviews": 100})
    agent = _make_agent("developer", bus)
    agent.publish_channels = ["review-requests"]

    env = _envelope(MessageType.REVIEW_REQUEST)
    agent.parse_response = lambda raw, src: [env]
    agent._prepare_and_publish_check = AsyncMock(return_value=True)
    agent._check_output_safety = AsyncMock(return_value=True)
    agent._route_outgoing = lambda e: "review-requests"
    agent._log_turn = lambda *a: None
    agent._metrics = None
    agent._publish_trace = AsyncMock()
    agent._check_stage_gate = AsyncMock(return_value=False)
    agent._is_paused = AsyncMock(return_value=False)
    agent.cli.send = AsyncMock(return_value="response")

    incoming = _envelope(MessageType.TASK_ASSIGNMENT)
    await agent._handle_message(incoming)

    # REVIEW_REQUEST should be published regardless of review queue depth
    bus.publish.assert_called_once()
    published_env = bus.publish.call_args[0][1]
    assert published_env.message_type == MessageType.REVIEW_REQUEST


@pytest.mark.asyncio
async def test_publish_task_allowed_when_both_below_limits():
    """TASK_ASSIGNMENT publishes when both task and review queues have room."""
    bus = _make_bus({"orchestrator:active:tasks": 2, "orchestrator:active:reviews": 4})
    agent = _make_agent("architect", bus)
    agent.publish_channels = ["tasks"]

    env = _envelope(MessageType.TASK_ASSIGNMENT)
    agent.parse_response = lambda raw, src: [env]
    agent._prepare_and_publish_check = AsyncMock(return_value=True)
    agent._check_output_safety = AsyncMock(return_value=True)
    agent._route_outgoing = lambda e: "tasks"
    agent._log_turn = lambda *a: None
    agent._metrics = None
    agent._publish_trace = AsyncMock()
    agent._check_stage_gate = AsyncMock(return_value=False)
    agent._is_paused = AsyncMock(return_value=False)
    agent.cli.send = AsyncMock(return_value="response")

    incoming = _envelope(MessageType.PROPOSAL)
    await agent._handle_message(incoming)

    bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_review_result_changes_requested_reopens_task_backlog():
    """A failed review should move the thread back to active task work."""
    bus = _make_bus({"orchestrator:active:reviews": 1})
    agent = _make_agent("reviewer", bus)
    env = _envelope(MessageType.REVIEW_RESULT, decision="changes_requested")
    agent.publish_channels = ["review-results"]
    agent.parse_response = lambda raw, src: [env]
    agent._prepare_and_publish_check = AsyncMock(return_value=True)
    agent._check_output_safety = AsyncMock(return_value=True)
    agent._route_outgoing = lambda e: "review-results"
    agent._log_turn = lambda *a: None
    agent._metrics = None
    agent._publish_trace = AsyncMock()
    agent._check_stage_gate = AsyncMock(return_value=False)
    agent._is_paused = AsyncMock(return_value=False)
    agent.cli.send = AsyncMock(return_value="response")

    incoming = _envelope(MessageType.REVIEW_REQUEST)
    await agent._handle_message(incoming)

    assert await bus.redis.scard("orchestrator:active:reviews") == 0
    assert await bus.redis.scard("orchestrator:active:tasks") == 1


@pytest.mark.asyncio
async def test_update_active_work_state_proposal_lifecycle():
    bus = _make_bus()
    agent = _make_agent("pm", bus)
    tid = "thread-1"

    proposal = Envelope(
        sender_id="pm-1",
        sender_role="pm",
        message_type=MessageType.PROPOSAL,
        payload={"title": "Test"},
        thread_id=tid,
    )
    await agent._update_active_work_state(proposal)
    assert await bus.redis.scard("orchestrator:active:proposals") == 1
    assert await bus.redis.sismember("orchestrator:active:proposals", tid) is True

    review = Envelope(
        sender_id="architect-1",
        sender_role="architect",
        message_type=MessageType.PROPOSAL_REVIEW,
        payload={"decision": "approved"},
        thread_id=tid,
    )
    await agent._update_active_work_state(review)
    assert await bus.redis.scard("orchestrator:active:proposals") == 0


@pytest.mark.asyncio
async def test_update_active_work_state_task_review_lifecycle():
    bus = _make_bus()
    agent = _make_agent("architect", bus)
    tid = "thread-2"

    task = Envelope(
        sender_id="architect-1",
        sender_role="architect",
        message_type=MessageType.TASK_ASSIGNMENT,
        payload={"branch_name": "agent/test"},
        thread_id=tid,
    )
    await agent._update_active_work_state(task)
    assert await bus.redis.scard("orchestrator:active:tasks") == 1

    review_request = Envelope(
        sender_id="developer-1",
        sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"branch_name": "agent/test"},
        thread_id=tid,
    )
    await agent._update_active_work_state(review_request)
    assert await bus.redis.scard("orchestrator:active:tasks") == 0
    assert await bus.redis.scard("orchestrator:active:reviews") == 1

    review_result = Envelope(
        sender_id="reviewer-1",
        sender_role="reviewer",
        message_type=MessageType.REVIEW_RESULT,
        payload={"decision": "approved"},
        thread_id=tid,
    )
    await agent._update_active_work_state(review_result)
    assert await bus.redis.scard("orchestrator:active:reviews") == 0
