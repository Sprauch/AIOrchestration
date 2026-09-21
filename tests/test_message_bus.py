"""Tests for MessageBus — Redis Streams pub/sub layer."""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus


def _make_envelope(**overrides) -> Envelope:
    defaults = dict(
        sender_id="test-1",
        sender_role="pm",
        message_type=MessageType.PROPOSAL,
        payload={"title": "Fix tests"},
        thread_id="thread-abc",
    )
    defaults.update(overrides)
    return Envelope(**defaults)


@pytest.fixture
def mock_redis():
    """Return a fully mocked async Redis client."""
    r = AsyncMock()
    r.ping = AsyncMock()
    r.aclose = AsyncMock()
    r.xadd = AsyncMock(return_value="1234567890-0")
    r.xread = AsyncMock(return_value=[])
    r.xreadgroup = AsyncMock(return_value=[])
    r.xack = AsyncMock()
    r.xrevrange = AsyncMock(return_value=[])
    r.xgroup_create = AsyncMock()
    return r


# ── connect() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_calls_from_url_and_ping(mock_redis):
    bus = MessageBus("redis://localhost:6379")
    with patch("agents.core.message_bus.aioredis.from_url", return_value=mock_redis) as from_url:
        await bus.connect()

    from_url.assert_called_once_with("redis://localhost:6379", decode_responses=True)
    mock_redis.ping.assert_awaited_once()
    assert bus.redis is mock_redis


@pytest.mark.asyncio
async def test_connect_propagates_connection_error(mock_redis):
    mock_redis.ping.side_effect = ConnectionError("refused")
    bus = MessageBus("redis://localhost:6379")
    with patch("agents.core.message_bus.aioredis.from_url", return_value=mock_redis):
        with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="refused"):
                await bus.connect()


@pytest.mark.asyncio
async def test_connect_retries_on_transient_ping_failure(mock_redis):
    """connect() retries when ping() raises a transient error, then succeeds."""
    mock_redis.ping.side_effect = [
        ConnectionError("refused"),
        ConnectionError("refused"),
        None,  # success on third attempt
    ]

    bus = MessageBus("redis://localhost:6379")
    with patch("agents.core.message_bus.aioredis.from_url", return_value=mock_redis) as from_url:
        with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
            await bus.connect(max_retries=3)

    # 3 from_url calls (one per attempt, since aclose resets client)
    assert from_url.call_count == 3
    assert mock_redis.ping.await_count == 3
    # Two retries -> two sleeps
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(0)
    mock_sleep.assert_any_await(1)
    assert bus.redis is mock_redis


@pytest.mark.asyncio
async def test_connect_retries_on_transient_from_url_failure():
    """connect() retries when from_url() raises a transient error."""
    good_redis = AsyncMock()
    good_redis.ping = AsyncMock()
    good_redis.aclose = AsyncMock()

    bus = MessageBus("redis://localhost:6379")
    with patch(
        "agents.core.message_bus.aioredis.from_url",
        side_effect=[
            ConnectionError("refused"),
            ConnectionError("refused"),
            good_redis,
        ],
    ) as from_url:
        with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
            await bus.connect(max_retries=3)

    assert from_url.call_count == 3
    good_redis.ping.assert_awaited_once()
    assert mock_sleep.await_count == 2
    assert bus.redis is good_redis


@pytest.mark.asyncio
async def test_connect_exhausts_retries_then_raises(mock_redis):
    """After exhausting retries, connect() re-raises the last transient error."""
    mock_redis.ping.side_effect = ConnectionError("still down")

    bus = MessageBus("redis://localhost:6379")
    with patch("agents.core.message_bus.aioredis.from_url", return_value=mock_redis):
        with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="still down"):
                await bus.connect(max_retries=2)

    # 1 initial + 2 retries = 3 total attempts
    assert mock_redis.ping.await_count == 3
    assert bus.redis is None


@pytest.mark.asyncio
async def test_connect_closes_client_before_retry(mock_redis):
    """Each failed attempt closes the client via aclose() before the next attempt."""
    mock_redis.ping.side_effect = [
        ConnectionError("refused"),
        None,  # success on second attempt
    ]

    bus = MessageBus("redis://localhost:6379")
    with patch("agents.core.message_bus.aioredis.from_url", return_value=mock_redis):
        with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
            await bus.connect(max_retries=2)

    # aclose called once for the failed first attempt
    mock_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_negative_retries_raises_value_error():
    """connect(max_retries=-1) raises ValueError immediately."""
    bus = MessageBus("redis://localhost:6379")
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        await bus.connect(max_retries=-1)


# ── close() ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_calls_aclose_and_clears_redis(mock_redis):
    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    await bus.close()

    mock_redis.aclose.assert_awaited_once()
    assert bus.redis is None


@pytest.mark.asyncio
async def test_close_noop_when_redis_is_none():
    bus = MessageBus("redis://localhost:6379")
    bus.redis = None

    await bus.close()  # should not raise
    assert bus.redis is None


# ── publish() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_xadd_with_maxlen(mock_redis):
    bus = MessageBus("redis://localhost:6379", stream_maxlen=10000)
    bus.redis = mock_redis
    env = _make_envelope()

    msg_id = await bus.publish("proposals", env)

    assert msg_id == "1234567890-0"
    mock_redis.xadd.assert_awaited_once_with(
        "stream:proposals",
        {"data": env.to_json()},
        maxlen=10000,
        approximate=True,
    )


@pytest.mark.asyncio
async def test_publish_xadd_without_maxlen(mock_redis):
    bus = MessageBus("redis://localhost:6379")  # no stream_maxlen
    bus.redis = mock_redis
    env = _make_envelope()

    await bus.publish("proposals", env)

    mock_redis.xadd.assert_awaited_once_with(
        "stream:proposals",
        {"data": env.to_json()},
    )


@pytest.mark.asyncio
async def test_publish_returns_message_id(mock_redis):
    mock_redis.xadd.return_value = "9999-1"
    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.publish("ch", _make_envelope())
    assert result == "9999-1"


# ── subscribe_simple() ────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_simple_default_last_ids(mock_redis):
    env = _make_envelope()
    captured_streams = []

    async def _capture_xread(streams, **kwargs):
        captured_streams.append(dict(streams))  # snapshot before mutation
        if len(captured_streams) == 1:
            return [("stream:proposals", [("1-0", {"data": env.to_json()})])]
        raise asyncio.CancelledError()

    mock_redis.xread.side_effect = _capture_xread

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with pytest.raises(asyncio.CancelledError):
        async for e in bus.subscribe_simple(["proposals"]):
            collected.append(e)

    assert len(collected) == 1
    assert collected[0].sender_id == env.sender_id
    assert collected[0].payload == env.payload

    # First call should use "0" as default start ID
    assert captured_streams[0] == {"stream:proposals": "0"}
    # After consuming message 1-0, the cursor should advance
    assert captured_streams[1] == {"stream:proposals": "1-0"}


@pytest.mark.asyncio
async def test_subscribe_simple_custom_last_ids(mock_redis):
    env = _make_envelope()
    captured_streams = []

    async def _capture_xread(streams, **kwargs):
        captured_streams.append(dict(streams))
        if len(captured_streams) == 1:
            return [("stream:tasks", [("5-0", {"data": env.to_json()})])]
        raise asyncio.CancelledError()

    mock_redis.xread.side_effect = _capture_xread

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with pytest.raises(asyncio.CancelledError):
        async for e in bus.subscribe_simple(["tasks"], last_ids={"tasks": "3-0"}):
            collected.append(e)

    assert captured_streams[0] == {"stream:tasks": "3-0"}


@pytest.mark.asyncio
async def test_subscribe_simple_skips_unparseable(mock_redis):
    """Bad data should be skipped, not crash the iterator."""
    env = _make_envelope()
    mock_redis.xread.side_effect = [
        [("stream:ch", [
            ("1-0", {"data": "NOT_VALID_JSON"}),
            ("2-0", {"data": env.to_json()}),
        ])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with pytest.raises(asyncio.CancelledError):
        async for e in bus.subscribe_simple(["ch"]):
            collected.append(e)

    assert len(collected) == 1
    assert collected[0].sender_id == env.sender_id


# ── subscribe_group() ─────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_group_creates_group_and_yields(mock_redis):
    env = _make_envelope()
    mock_redis.xreadgroup.side_effect = [
        [("stream:tasks", [("10-0", {"data": env.to_json()})])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with pytest.raises(asyncio.CancelledError):
        async for msg_id, e in bus.subscribe_group("tasks", "workers", "w1"):
            collected.append((msg_id, e))

    mock_redis.xgroup_create.assert_awaited_once_with(
        "stream:tasks", "workers", id="0", mkstream=True,
    )
    assert len(collected) == 1
    assert collected[0][0] == "10-0"
    assert collected[0][1].sender_id == env.sender_id


@pytest.mark.asyncio
async def test_subscribe_group_ignores_busygroup_error(mock_redis):
    """BUSYGROUP means the group already exists — should be silently ignored."""
    import redis.asyncio as aioredis

    mock_redis.xgroup_create.side_effect = aioredis.ResponseError("BUSYGROUP group already exists")
    env = _make_envelope()
    mock_redis.xreadgroup.side_effect = [
        [("stream:tasks", [("1-0", {"data": env.to_json()})])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with pytest.raises(asyncio.CancelledError):
        async for msg_id, e in bus.subscribe_group("tasks", "grp", "c1"):
            collected.append((msg_id, e))

    assert len(collected) == 1


@pytest.mark.asyncio
async def test_subscribe_group_propagates_non_busygroup_error(mock_redis):
    import redis.asyncio as aioredis

    mock_redis.xgroup_create.side_effect = aioredis.ResponseError("WRONGTYPE other error")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with pytest.raises(aioredis.ResponseError, match="WRONGTYPE"):
        async for _ in bus.subscribe_group("tasks", "grp", "c1"):
            pass


@pytest.mark.asyncio
async def test_subscribe_group_acks_unparseable_messages(mock_redis):
    """Bad messages should be acked so they don't block the consumer."""
    mock_redis.xreadgroup.side_effect = [
        [("stream:tasks", [("bad-1", {"data": "INVALID"})])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with pytest.raises(asyncio.CancelledError):
        async for msg_id, e in bus.subscribe_group("tasks", "grp", "c1"):
            collected.append((msg_id, e))

    assert len(collected) == 0
    mock_redis.xack.assert_awaited_once_with("stream:tasks", "grp", "bad-1")


# ── ack() ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ack_calls_xack(mock_redis):
    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    await bus.ack("tasks", "workers", "42-0")

    mock_redis.xack.assert_awaited_once_with("stream:tasks", "workers", "42-0")


# ── get_history() ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_history_returns_envelopes_in_order(mock_redis):
    e1 = _make_envelope(sender_id="a")
    e2 = _make_envelope(sender_id="b")
    # xrevrange returns newest first; get_history reverses
    mock_redis.xrevrange.return_value = [
        ("2-0", {"data": e2.to_json()}),
        ("1-0", {"data": e1.to_json()}),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.get_history("proposals", count=50)

    mock_redis.xrevrange.assert_awaited_once_with("stream:proposals", count=50)
    assert len(result) == 2
    assert result[0].sender_id == "a"
    assert result[1].sender_id == "b"


@pytest.mark.asyncio
async def test_get_history_skips_bad_messages(mock_redis):
    env = _make_envelope()
    mock_redis.xrevrange.return_value = [
        ("2-0", {"data": "BAD_JSON"}),
        ("1-0", {"data": env.to_json()}),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.get_history("ch")
    assert len(result) == 1
    assert result[0].sender_id == env.sender_id


@pytest.mark.asyncio
async def test_get_history_empty_stream(mock_redis):
    mock_redis.xrevrange.return_value = []

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.get_history("empty")
    assert result == []


# ── wait_for_message() ────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_message_returns_envelope(mock_redis):
    env = _make_envelope()
    mock_redis.xread.return_value = [
        ("stream:gate", [("1-0", {"data": env.to_json()})]),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.wait_for_message("gate", timeout_ms=5000)

    assert result is not None
    assert result.sender_id == env.sender_id
    # With monotonic deadline, block value will be close to 5000 but not exact
    call_args = mock_redis.xread.call_args
    assert call_args[0][0] == {"stream:gate": "$"}
    assert call_args[1]["count"] == 1
    assert 4900 <= call_args[1]["block"] <= 5000


@pytest.mark.asyncio
async def test_wait_for_message_returns_none_on_timeout(mock_redis):
    mock_redis.xread.return_value = []

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.wait_for_message("gate", timeout_ms=100)
    assert result is None


@pytest.mark.asyncio
async def test_wait_for_message_custom_last_id(mock_redis):
    mock_redis.xread.return_value = []

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    await bus.wait_for_message("gate", timeout_ms=100, last_id="5-0")

    call_args = mock_redis.xread.call_args
    assert call_args[0][0] == {"stream:gate": "5-0"}
    assert call_args[1]["count"] == 1
    assert 1 <= call_args[1]["block"] <= 100


@pytest.mark.asyncio
async def test_wait_for_message_returns_none_on_bad_data(mock_redis):
    mock_redis.xread.return_value = [
        ("stream:gate", [("1-0", {"data": "NOT_JSON"})]),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    result = await bus.wait_for_message("gate", timeout_ms=100)
    assert result is None


# ── Reconnect / backoff tests ─────────────────────────────


@pytest.mark.asyncio
async def test_publish_retries_on_connection_error(mock_redis):
    """publish() should retry on ConnectionError and succeed once Redis is back."""
    env = _make_envelope()
    mock_redis.xadd.side_effect = [
        ConnectionError("conn refused"),
        ConnectionError("conn refused"),
        "99-0",  # success on third call
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
        with patch.object(bus, "_reconnect", new_callable=AsyncMock):
            result = await bus.publish("proposals", env)

    assert result == "99-0"
    assert mock_redis.xadd.await_count == 3
    # Two retries -> two sleeps
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(0)
    mock_sleep.assert_any_await(1)


@pytest.mark.asyncio
async def test_publish_does_not_retry_response_error(mock_redis):
    """Non-connection errors (e.g., ResponseError) must propagate immediately."""
    import redis.asyncio as aioredis

    env = _make_envelope()
    mock_redis.xadd.side_effect = aioredis.ResponseError("WRONGTYPE bad command")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(aioredis.ResponseError, match="WRONGTYPE"):
            await bus.publish("ch", env)

    # No retries — should fail immediately
    assert mock_redis.xadd.await_count == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_raises_after_max_retries(mock_redis):
    """publish() should raise after exhausting max_retries."""
    env = _make_envelope()
    mock_redis.xadd.side_effect = ConnectionError("connection refused")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="connection refused"):
                await bus.publish("ch", env, max_retries=3)

    # 1 initial attempt + 3 retries = 4 total calls
    assert mock_redis.xadd.await_count == 4


@pytest.mark.asyncio
async def test_publish_calls_reconnect_on_retry(mock_redis):
    """publish() should call _reconnect between retries."""
    env = _make_envelope()
    mock_redis.xadd.side_effect = [
        ConnectionError("down"),
        "ok-1",
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock) as mock_reconnect:
            result = await bus.publish("ch", env)

    assert result == "ok-1"
    mock_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_retries_on_timeout_error(mock_redis):
    """TimeoutError should also be retried as a transient error."""
    env = _make_envelope()
    mock_redis.xadd.side_effect = [
        TimeoutError("timed out"),
        "42-0",
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock):
            result = await bus.publish("ch", env)

    assert result == "42-0"
    assert mock_redis.xadd.await_count == 2


@pytest.mark.asyncio
async def test_subscribe_simple_retries_on_connection_error(mock_redis):
    """subscribe_simple should retry on ConnectionError, then resume from last ID."""
    env = _make_envelope()
    call_count = 0

    async def _xread_side_effect(streams, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call succeeds with a message
            return [("stream:ch", [("5-0", {"data": env.to_json()})])]
        if call_count == 2:
            # Second call: Redis goes down
            raise ConnectionError("connection lost")
        if call_count == 3:
            # Third call: Redis goes down again
            raise ConnectionError("still down")
        if call_count == 4:
            # Fourth call: Redis is back — verify it resumes from "5-0"
            assert streams.get("stream:ch") == "5-0"
            return [("stream:ch", [("6-0", {"data": env.to_json()})])]
        raise asyncio.CancelledError()

    mock_redis.xread.side_effect = _xread_side_effect

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
        with patch.object(bus, "_reconnect", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                async for e in bus.subscribe_simple(["ch"]):
                    collected.append(e)

    assert len(collected) == 2
    # Two connection errors -> two backoff sleeps
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_subscribe_simple_does_not_retry_response_error(mock_redis):
    """Non-transient errors propagate immediately from subscribe_simple."""
    import redis.asyncio as aioredis

    mock_redis.xread.side_effect = aioredis.ResponseError("WRONGTYPE")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(aioredis.ResponseError, match="WRONGTYPE"):
            async for _ in bus.subscribe_simple(["ch"]):
                pass

    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscribe_simple_calls_reconnect(mock_redis):
    """subscribe_simple should call _reconnect after transient failure."""
    env = _make_envelope()
    call_count = 0

    async def _xread(streams, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("lost")
        if call_count == 2:
            return [("stream:ch", [("1-0", {"data": env.to_json()})])]
        raise asyncio.CancelledError()

    mock_redis.xread.side_effect = _xread

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(asyncio.CancelledError):
                async for e in bus.subscribe_simple(["ch"]):
                    collected.append(e)

    assert len(collected) == 1
    mock_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscribe_group_retries_on_connection_error(mock_redis):
    """subscribe_group should retry xreadgroup on ConnectionError."""
    env = _make_envelope()
    mock_redis.xreadgroup.side_effect = [
        ConnectionError("connection lost"),
        ConnectionError("still down"),
        [("stream:tasks", [("10-0", {"data": env.to_json()})])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
        with patch.object(bus, "_reconnect", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                async for msg_id, e in bus.subscribe_group("tasks", "workers", "w1"):
                    collected.append((msg_id, e))

    assert len(collected) == 1
    assert collected[0][0] == "10-0"
    # Two connection errors -> two backoff sleeps
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(0)
    mock_sleep.assert_any_await(1)


@pytest.mark.asyncio
async def test_subscribe_group_does_not_retry_response_error(mock_redis):
    """Non-transient errors propagate immediately from subscribe_group."""
    import redis.asyncio as aioredis

    mock_redis.xreadgroup.side_effect = aioredis.ResponseError("NOGROUP")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(aioredis.ResponseError, match="NOGROUP"):
            async for _ in bus.subscribe_group("tasks", "grp", "c1"):
                pass

    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscribe_group_calls_reconnect(mock_redis):
    """subscribe_group should call _reconnect after transient failure."""
    env = _make_envelope()
    mock_redis.xreadgroup.side_effect = [
        ConnectionError("lost"),
        [("stream:tasks", [("1-0", {"data": env.to_json()})])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    collected = []
    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(asyncio.CancelledError):
                async for msg_id, e in bus.subscribe_group("tasks", "grp", "c1"):
                    collected.append((msg_id, e))

    assert len(collected) == 1
    mock_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_message_retries_on_connection_error(mock_redis):
    """wait_for_message should retry on transient errors within the deadline."""
    env = _make_envelope()
    mock_redis.xread.side_effect = [
        ConnectionError("lost"),
        [("stream:gate", [("1-0", {"data": env.to_json()})])],
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock):
            result = await bus.wait_for_message("gate", timeout_ms=10000)

    assert result is not None
    assert result.sender_id == env.sender_id
    assert mock_redis.xread.await_count == 2


@pytest.mark.asyncio
async def test_wait_for_message_returns_none_when_deadline_exceeded(mock_redis):
    """wait_for_message should return None if reconnect backoff exceeds deadline."""
    import time

    mock_redis.xread.side_effect = ConnectionError("lost")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    original_monotonic = time.monotonic
    call_count = 0

    def _advancing_monotonic():
        nonlocal call_count
        call_count += 1
        # First two calls set up the deadline and initial check
        if call_count <= 2:
            return original_monotonic()
        # After the error, advance past deadline
        return original_monotonic() + 100

    with patch("agents.core.message_bus.time.monotonic", side_effect=_advancing_monotonic):
        with patch("agents.core.message_bus.asyncio.sleep", new_callable=AsyncMock):
            with patch.object(bus, "_reconnect", new_callable=AsyncMock):
                result = await bus.wait_for_message("gate", timeout_ms=500)

    assert result is None


@pytest.mark.asyncio
async def test_backoff_sleep_values():
    """Verify backoff is exponential with jitter, capped at 30s."""
    from agents.core.message_bus import _backoff_sleep

    sleeps = []

    async def _capture_sleep(duration):
        sleeps.append(duration)

    with patch("agents.core.message_bus.asyncio.sleep", side_effect=_capture_sleep):
        with patch("agents.core.message_bus.random.uniform", side_effect=lambda lo, hi: hi):
            # attempt 0: min(1*2^0, 30) = 1.0; jitter returns hi=1.0
            await _backoff_sleep(0)
            # attempt 1: min(1*2^1, 30) = 2.0
            await _backoff_sleep(1)
            # attempt 2: min(1*2^2, 30) = 4.0
            await _backoff_sleep(2)
            # attempt 5: min(1*2^5, 30) = 30.0 (capped)
            await _backoff_sleep(5)
            # attempt 10: min(1*2^10, 30) = 30.0 (capped)
            await _backoff_sleep(10)

    assert sleeps == [1.0, 2.0, 4.0, 30.0, 30.0]


@pytest.mark.asyncio
async def test_reconnect_creates_new_connection(mock_redis):
    """_reconnect should close old connection and create a new one."""
    new_redis = AsyncMock()
    new_redis.ping = AsyncMock()

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus.aioredis.from_url", return_value=new_redis) as from_url:
        await bus._reconnect()

    mock_redis.aclose.assert_awaited_once()
    from_url.assert_called_once_with("redis://localhost:6379", decode_responses=True)
    new_redis.ping.assert_awaited_once()
    assert bus.redis is new_redis


@pytest.mark.asyncio
async def test_reconnect_ignores_close_error():
    """_reconnect should not fail if closing the old connection raises."""
    old_redis = AsyncMock()
    old_redis.aclose.side_effect = OSError("already closed")
    new_redis = AsyncMock()
    new_redis.ping = AsyncMock()

    bus = MessageBus("redis://localhost:6379")
    bus.redis = old_redis

    with patch("agents.core.message_bus.aioredis.from_url", return_value=new_redis):
        await bus._reconnect()  # should not raise

    assert bus.redis is new_redis


# ── _reconnect() failure wrapping tests ──────────────────


@pytest.mark.asyncio
async def test_publish_reconnect_transient_failure_retries_full_count(mock_redis):
    """If _reconnect() raises a transient error, publish() still retries the full count."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    env = _make_envelope()
    mock_redis.xadd.side_effect = ConnectionError("connection refused")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(
            bus, "_reconnect", new_callable=AsyncMock,
            side_effect=RedisConnectionError("reconnect failed"),
        ):
            with pytest.raises(ConnectionError, match="connection refused"):
                await bus.publish("ch", env, max_retries=3)

    # 1 initial + 3 retries = 4 total xadd calls
    assert mock_redis.xadd.await_count == 4


@pytest.mark.asyncio
async def test_publish_reconnect_non_transient_propagates(mock_redis):
    """If _reconnect() raises a non-transient error, it propagates immediately."""
    import redis.asyncio as aioredis

    env = _make_envelope()
    mock_redis.xadd.side_effect = ConnectionError("connection refused")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(
            bus, "_reconnect", new_callable=AsyncMock,
            side_effect=aioredis.ResponseError("AUTH required"),
        ):
            with pytest.raises(aioredis.ResponseError, match="AUTH required"):
                await bus.publish("ch", env, max_retries=3)

    # Only 1 xadd call — reconnect failure stops the loop
    assert mock_redis.xadd.await_count == 1


@pytest.mark.asyncio
async def test_subscribe_simple_reconnect_transient_failure_continues(mock_redis):
    """subscribe_simple continues looping when _reconnect() raises a transient error."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    env = _make_envelope()
    call_count = 0

    async def _xread(streams, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("down")
        if call_count == 3:
            return [("stream:ch", [("1-0", {"data": env.to_json()})])]
        raise asyncio.CancelledError()

    mock_redis.xread.side_effect = _xread

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    reconnect_count = 0

    async def _failing_reconnect():
        nonlocal reconnect_count
        reconnect_count += 1
        if reconnect_count == 1:
            raise RedisConnectionError("reconnect failed")
        # Second reconnect succeeds

    collected = []
    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock, side_effect=_failing_reconnect):
            with pytest.raises(asyncio.CancelledError):
                async for e in bus.subscribe_simple(["ch"]):
                    collected.append(e)

    assert len(collected) == 1
    assert reconnect_count == 2


@pytest.mark.asyncio
async def test_subscribe_simple_reconnect_non_transient_propagates(mock_redis):
    """subscribe_simple propagates non-transient _reconnect() errors immediately."""
    import redis.asyncio as aioredis

    mock_redis.xread.side_effect = ConnectionError("down")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(
            bus, "_reconnect", new_callable=AsyncMock,
            side_effect=aioredis.ResponseError("AUTH required"),
        ):
            with pytest.raises(aioredis.ResponseError, match="AUTH required"):
                async for _ in bus.subscribe_simple(["ch"]):
                    pass


@pytest.mark.asyncio
async def test_subscribe_group_reconnect_transient_failure_continues(mock_redis):
    """subscribe_group continues looping when _reconnect() raises a transient error."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    env = _make_envelope()
    mock_redis.xreadgroup.side_effect = [
        ConnectionError("down"),
        ConnectionError("still down"),
        [("stream:tasks", [("10-0", {"data": env.to_json()})])],
        asyncio.CancelledError(),
    ]

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    reconnect_count = 0

    async def _failing_reconnect():
        nonlocal reconnect_count
        reconnect_count += 1
        if reconnect_count == 1:
            raise RedisConnectionError("reconnect failed")

    collected = []
    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(bus, "_reconnect", new_callable=AsyncMock, side_effect=_failing_reconnect):
            with pytest.raises(asyncio.CancelledError):
                async for msg_id, e in bus.subscribe_group("tasks", "grp", "c1"):
                    collected.append((msg_id, e))

    assert len(collected) == 1
    assert collected[0][0] == "10-0"
    assert reconnect_count == 2


@pytest.mark.asyncio
async def test_subscribe_group_reconnect_non_transient_propagates(mock_redis):
    """subscribe_group propagates non-transient _reconnect() errors immediately."""
    import redis.asyncio as aioredis

    mock_redis.xreadgroup.side_effect = ConnectionError("down")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus._backoff_sleep", new_callable=AsyncMock):
        with patch.object(
            bus, "_reconnect", new_callable=AsyncMock,
            side_effect=aioredis.ResponseError("AUTH required"),
        ):
            with pytest.raises(aioredis.ResponseError, match="AUTH required"):
                async for _ in bus.subscribe_group("tasks", "grp", "c1"):
                    pass


@pytest.mark.asyncio
async def test_wait_for_message_reconnect_transient_failure_respects_deadline(mock_redis):
    """wait_for_message continues after transient _reconnect() failure, respecting deadline."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    env = _make_envelope()
    xread_count = 0

    async def _xread(streams, **kwargs):
        nonlocal xread_count
        xread_count += 1
        if xread_count == 1:
            raise ConnectionError("down")
        return [("stream:gate", [("1-0", {"data": env.to_json()})])]

    mock_redis.xread.side_effect = _xread

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(
            bus, "_reconnect", new_callable=AsyncMock,
            side_effect=RedisConnectionError("reconnect failed"),
        ):
            result = await bus.wait_for_message("gate", timeout_ms=10000)

    assert result is not None
    assert result.sender_id == env.sender_id
    assert xread_count == 2


@pytest.mark.asyncio
async def test_wait_for_message_reconnect_non_transient_propagates(mock_redis):
    """wait_for_message propagates non-transient _reconnect() errors immediately."""
    import redis.asyncio as aioredis

    mock_redis.xread.side_effect = ConnectionError("down")

    bus = MessageBus("redis://localhost:6379")
    bus.redis = mock_redis

    with patch("agents.core.message_bus.asyncio.sleep", new_callable=AsyncMock):
        with patch.object(
            bus, "_reconnect", new_callable=AsyncMock,
            side_effect=aioredis.ResponseError("AUTH required"),
        ):
            with pytest.raises(aioredis.ResponseError, match="AUTH required"):
                await bus.wait_for_message("gate", timeout_ms=10000)


# ── Long waits must survive the connection's own read timeout ────────────


def test_redis_timeout_is_treated_as_transient():
    """redis.exceptions.TimeoutError is not a builtin TimeoutError.

    It derives from RedisError, so a transient set listing only the builtin excluded the
    one timeout this layer actually raises. A timed-out read was then treated as a
    permanent fault and re-raised, which killed the message being handled instead of
    retrying — that is how an approved gate lost the task it had been holding.
    """
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from agents.core.message_bus import _is_transient

    assert _is_transient(RedisTimeoutError("Timeout reading from localhost:6379"))
    assert not isinstance(RedisTimeoutError("x"), TimeoutError)


@pytest.mark.asyncio
async def test_wait_for_message_slices_a_long_block():
    """A week-long wait must not become a week-long XREAD.

    The gate timeout belongs to the user; the connection has its own read timeout and
    does not care. Asking Redis to block for the whole week raised TimeoutError about five
    seconds in. Each read is now capped, and an empty read CONTINUES the wait rather than
    ending it — the deadline is the only thing that ends it.
    """
    from agents.core.message_bus import _MAX_BLOCK_MS, MessageBus
    from agents.core.message import Envelope, MessageType

    blocks = []
    reply = Envelope(
        sender_id="user", sender_role="user",
        message_type=MessageType.SYSTEM,
        payload={"action": "approval_granted"},
    )

    class FakeRedis:
        async def xread(self, streams, block=None, count=None):
            blocks.append(block)
            if len(blocks) < 3:
                return []  # this slice expired; the wait must go on
            return [("stream:gate-responses:abc", [("1-1", {"data": reply.to_json()})])]

    bus = MessageBus("redis://unused")
    bus.redis = FakeRedis()

    got = await bus.wait_for_message("gate-responses:abc", timeout_ms=604_800_000)

    assert got is not None                     # the answer arrived after empty slices
    assert got.payload["action"] == "approval_granted"
    assert len(blocks) == 3                    # two empty reads did not end the wait
    assert max(blocks) <= _MAX_BLOCK_MS        # never asked for more than a slice
