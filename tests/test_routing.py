"""Tests for agent message routing and filtering."""

from agents.core.message import Envelope, MessageType
from agents.roles.pm_agent import PMAgent
from agents.roles.architect_agent import ArchitectAgent
from agents.roles.developer_agent import DeveloperAgent
from agents.roles.reviewer_agent import ReviewerAgent


class FakeCLI:
    async def send(self, prompt: str) -> str:
        return '{"proposals": [{"title": "test", "priority": 3}]}'

    async def resume(self):
        pass


class FakeBus:
    def __init__(self):
        self.published = []
        self.redis = FakeRedis()

    async def connect(self):
        pass

    async def close(self):
        pass

    async def publish(self, channel, envelope):
        self.published.append((channel, envelope))

    async def subscribe_simple(self, channels, last_ids=None):
        return
        yield  # make it an async generator

    async def get_history(self, channel, count=100):
        return []


class FakeRedis:
    async def set(self, key, value):
        pass

    async def get(self, key):
        return None

    async def xlen(self, key):
        return 0


def test_pm_default_channels():
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    channels = agent.default_channels()
    assert "system" in channels["subscribes_to"]
    assert "proposals" in channels["publishes_to"]


def test_architect_default_channels():
    agent = ArchitectAgent(
        agent_id="arch-1", role="architect",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    channels = agent.default_channels()
    assert "proposals" in channels["subscribes_to"]
    assert "reviews" in channels["publishes_to"]
    assert "tasks" in channels["publishes_to"]


def test_developer_default_channels():
    agent = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    channels = agent.default_channels()
    assert "tasks" in channels["subscribes_to"]
    assert "review-requests" in channels["publishes_to"]


def test_reviewer_default_channels():
    agent = ReviewerAgent(
        agent_id="rev-1", role="reviewer",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    channels = agent.default_channels()
    assert "review-requests" in channels["subscribes_to"]
    assert "review-results" in channels["publishes_to"]


def test_should_process_matching_role():
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    env = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
        recipient_role="pm",
    )
    assert agent._should_process(env) is True


def test_should_process_wrong_role():
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    env = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
        recipient_role="developer",
    )
    assert agent._should_process(env) is False


def test_should_process_no_recipient():
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    env = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
    )
    assert agent._should_process(env) is True


def test_should_process_target_agent_match():
    agent = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    env = Envelope(
        sender_id="reviewer-1",
        sender_role="reviewer",
        message_type=MessageType.REVIEW_RESULT,
        payload={"_target_agent_id": "dev-1"},
    )
    assert agent._should_process(env) is True


def test_should_process_target_agent_mismatch():
    agent = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    env = Envelope(
        sender_id="reviewer-1",
        sender_role="reviewer",
        message_type=MessageType.REVIEW_RESULT,
        payload={"_target_agent_id": "dev-2"},
    )
    # dev-2 is in same role group as dev-1, so this should be filtered
    assert agent._should_process(env) is False


def test_pm_parse_response_structured():
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    source = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
    )
    raw = '```json\n{"proposals": [{"title": "Add tests", "priority": 2, "affected_files": ["app/main.py"], "estimated_effort": "small", "category": "tests"}]}\n```'
    envelopes = agent.parse_response(raw, source)
    assert len(envelopes) == 1
    assert envelopes[0].message_type == MessageType.PROPOSAL
    assert envelopes[0].payload["title"] == "Add tests"


def test_pm_parse_response_fallback_drops_garbage():
    """When the PM can't parse structured output, it should return nothing — not publish garbage."""
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(), bus=FakeBus(),
    )
    source = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
    )
    raw = "This is just plain text with no JSON."
    envelopes = agent.parse_response(raw, source)
    assert len(envelopes) == 0
