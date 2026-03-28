"""Tests for consumer-group multi-channel consumption in AgentProcess."""

import asyncio

import pytest

from agents.core.base_agent import AgentProcess
from agents.core.message import Envelope, MessageType


class FakeCLI:
    async def send(self, prompt: str) -> str:
        return '{"status": "ok"}'

    async def resume(self):
        pass


class FakeRedis:
    async def set(self, key, value):
        pass

    async def get(self, key):
        return None


class FakeGroupBus:
    """A fake MessageBus that yields pre-loaded messages per channel via subscribe_group."""

    def __init__(self):
        self.redis = FakeRedis()
        self._queues: dict[str, asyncio.Queue] = {}
        self.acked: list[tuple[str, str, str]] = []
        self.published: list[tuple[str, Envelope]] = []

    def load(self, channel: str, messages: list[tuple[str, Envelope]]) -> None:
        q: asyncio.Queue = self._queues.setdefault(channel, asyncio.Queue())
        for msg_id, env in messages:
            q.put_nowait((msg_id, env))

    async def subscribe_group(self, channel, group, consumer, min_idle_time=30_000):
        q = self._queues.get(channel, asyncio.Queue())
        while True:
            try:
                msg_id, envelope = q.get_nowait()
                yield msg_id, envelope
            except asyncio.QueueEmpty:
                return

    async def ack(self, channel, group, msg_id):
        self.acked.append((channel, group, msg_id))

    async def publish(self, channel, envelope):
        self.published.append((channel, envelope))

    async def connect(self):
        pass

    async def close(self):
        pass


class StubAgent(AgentProcess):
    """Minimal concrete AgentProcess for testing."""

    def __init__(self, **kwargs):
        self.handled: list[Envelope] = []
        super().__init__(**kwargs)

    def default_channels(self):
        return {"subscribes_to": [], "publishes_to": []}

    def format_prompt(self, envelope):
        return envelope.payload.get("text", "")

    def parse_response(self, raw, source_envelope):
        return []

    async def _handle_message(self, envelope):
        """Record the envelope instead of invoking the CLI."""
        self.handled.append(envelope)


def _make_envelope(channel_tag: str, idx: int) -> Envelope:
    return Envelope(
        sender_id="test",
        sender_role="system",
        message_type=MessageType.SYSTEM,
        payload={"channel": channel_tag, "index": idx},
    )


@pytest.mark.asyncio
async def test_consumer_group_receives_from_multiple_channels():
    """An agent subscribed to 2 channels should process messages from both."""
    bus = FakeGroupBus()

    env_a1 = _make_envelope("alpha", 1)
    env_a2 = _make_envelope("alpha", 2)
    env_b1 = _make_envelope("beta", 1)

    bus.load("alpha", [("a-1", env_a1), ("a-2", env_a2)])
    bus.load("beta", [("b-1", env_b1)])

    agent = StubAgent(
        agent_id="test-1",
        role="tester",
        cli_session=FakeCLI(),
        bus=bus,
        use_consumer_group=True,
        config_channels={
            "subscribes_to": ["alpha", "beta"],
            "publishes_to": [],
        },
    )

    # start() calls _run_consumer_group; it will exit when queues drain
    await agent.start()

    # All three messages from both channels should be handled
    assert len(agent.handled) == 3
    channels_seen = {e.payload["channel"] for e in agent.handled}
    assert channels_seen == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_consumer_group_acks_per_channel():
    """Each message should be acked on its own channel and group."""
    bus = FakeGroupBus()

    env_a = _make_envelope("alpha", 1)
    env_b = _make_envelope("beta", 1)
    bus.load("alpha", [("a-1", env_a)])
    bus.load("beta", [("b-1", env_b)])

    agent = StubAgent(
        agent_id="test-1",
        role="tester",
        cli_session=FakeCLI(),
        bus=bus,
        use_consumer_group=True,
        config_channels={
            "subscribes_to": ["alpha", "beta"],
            "publishes_to": [],
        },
    )

    await agent.start()

    # Verify acks happened on correct channels
    ack_channels = {ch for ch, _grp, _mid in bus.acked}
    assert "alpha" in ack_channels
    assert "beta" in ack_channels

    # Verify group name
    for _ch, grp, _mid in bus.acked:
        assert grp == "tester-group"


@pytest.mark.asyncio
async def test_consumer_group_empty_channel_does_not_block():
    """If one channel has no messages, the other should still be consumed."""
    bus = FakeGroupBus()

    env_b = _make_envelope("beta", 1)
    bus.load("beta", [("b-1", env_b)])
    # alpha has no messages loaded

    agent = StubAgent(
        agent_id="test-1",
        role="tester",
        cli_session=FakeCLI(),
        bus=bus,
        use_consumer_group=True,
        config_channels={
            "subscribes_to": ["alpha", "beta"],
            "publishes_to": [],
        },
    )

    await agent.start()

    assert len(agent.handled) == 1
    assert agent.handled[0].payload["channel"] == "beta"


@pytest.mark.asyncio
async def test_consumer_group_skipped_messages_still_acked():
    """Messages filtered by _should_process should still be acked."""
    bus = FakeGroupBus()

    # This envelope targets a different agent in the same role group
    env = Envelope(
        sender_id="system",
        sender_role="system",
        message_type=MessageType.SYSTEM,
        payload={"_target_agent_id": "tester-2"},
    )
    bus.load("alpha", [("a-1", env)])

    agent = StubAgent(
        agent_id="tester-1",
        role="tester",
        cli_session=FakeCLI(),
        bus=bus,
        use_consumer_group=True,
        config_channels={
            "subscribes_to": ["alpha"],
            "publishes_to": [],
        },
    )

    await agent.start()

    # Message should be skipped but acked
    assert len(agent.handled) == 0
    assert len(bus.acked) == 1
    assert bus.acked[0] == ("alpha", "tester-group", "a-1")
