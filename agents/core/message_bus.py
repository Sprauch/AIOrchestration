"""MessageBus — Redis Streams pub/sub layer for inter-agent communication."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import AsyncIterator

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from agents.core.message import Envelope

logger = logging.getLogger(__name__)

# Backoff constants for transient Redis errors.
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_CAP = 30.0  # max delay in seconds
_DEFAULT_MAX_RETRIES = 5

# Exception types considered transient (connection-level).
_TRANSIENT_ERRORS = (RedisConnectionError, ConnectionError, TimeoutError, OSError)


def _is_transient(exc: BaseException) -> bool:
    """Return True if the exception looks like a transient Redis connection error."""
    return isinstance(exc, _TRANSIENT_ERRORS)


async def _backoff_sleep(attempt: int) -> float:
    """Sleep with exponential backoff + full jitter, capped at _BACKOFF_CAP.

    Returns the actual delay slept (useful for deadline tracking).
    """
    delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
    jittered = random.uniform(0, delay)  # noqa: S311 — not security-sensitive
    await asyncio.sleep(jittered)
    return jittered


class MessageBus:
    """Thin wrapper around Redis Streams for publishing and subscribing."""

    def __init__(self, redis_url: str, stream_maxlen: int | None = None):
        self.redis_url = redis_url
        self.redis: aioredis.Redis | None = None
        self.stream_maxlen = stream_maxlen

    async def connect(self, *, max_retries: int = _DEFAULT_MAX_RETRIES) -> None:
        """Connect to Redis with retry + exponential backoff on transient errors.

        Makes 1 initial attempt + up to *max_retries* retries (N+1 total),
        matching :meth:`publish` semantics.
        """
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")

        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
                await self.redis.ping()
                logger.info("Connected to Redis at %s", self.redis_url)
                return
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                last_exc = exc
                # Close the failed client before retrying
                try:
                    if self.redis:
                        await self.redis.aclose()
                except Exception:
                    pass
                self.redis = None
                if attempt < max_retries:
                    logger.warning(
                        "connect() failed (attempt %d/%d): %s",
                        attempt + 1, max_retries + 1, exc,
                    )
                    await _backoff_sleep(attempt)

        raise last_exc  # type: ignore[misc]

    async def _reconnect(self) -> None:
        """Re-establish the Redis connection after a transient failure."""
        logger.info("Reconnecting to Redis at %s", self.redis_url)
        try:
            if self.redis:
                await self.redis.aclose()
        except Exception:
            pass
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        await self.redis.ping()
        logger.info("Reconnected to Redis at %s", self.redis_url)

    async def _ensure_connected(self) -> None:
        """Ensure a live Redis client exists before issuing commands."""
        if self.redis is None:
            await self.connect()

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()
            self.redis = None

    async def publish(
        self,
        channel: str,
        envelope: Envelope,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> str:
        """Publish an envelope to a Redis stream. Returns the message ID.

        Retries with exponential backoff on transient connection errors up to
        *max_retries* times. Non-connection errors propagate immediately.

        NOTE: If XADD succeeds on the server but the response is lost due to a
        connection drop, the retry will produce a duplicate message in the stream.
        Consumers must be prepared for at-least-once delivery.
        """
        stream_key = f"stream:{channel}"
        kwargs = {}
        if self.stream_maxlen:
            kwargs["maxlen"] = self.stream_maxlen
            kwargs["approximate"] = True

        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                await self._ensure_connected()
                msg_id = await self.redis.xadd(
                    stream_key, {"data": envelope.to_json()}, **kwargs,
                )
                logger.debug(
                    "Published %s to %s (id=%s)",
                    envelope.message_type.value, channel, msg_id,
                )
                return msg_id
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                last_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "publish to %s failed (attempt %d/%d): %s",
                        channel, attempt + 1, max_retries + 1, exc,
                    )
                    await _backoff_sleep(attempt)
                    try:
                        await self._reconnect()
                    except Exception as re_exc:
                        if not _is_transient(re_exc):
                            raise
                        logger.warning(
                            "publish reconnect failed (attempt %d/%d): %s",
                            attempt + 1, max_retries + 1, re_exc,
                        )

        raise last_exc  # type: ignore[misc]

    async def subscribe_simple(
        self,
        channels: list[str],
        last_ids: dict[str, str] | None = None,
    ) -> AsyncIterator[Envelope]:
        """Yield envelopes from multiple streams using XREAD (no consumer groups).

        last_ids maps channel name -> last seen ID. Defaults to "0" (replay all).
        Use "$" to get only new messages.

        On transient connection errors the subscriber reconnects with exponential
        backoff and resumes from the last successfully consumed cursor position —
        no messages are skipped or replayed.
        """
        streams = {}
        for ch in channels:
            start_id = (last_ids or {}).get(ch, "0")
            streams[f"stream:{ch}"] = start_id

        attempt = 0
        while True:
            try:
                await self._ensure_connected()
                results = await self.redis.xread(streams, block=2000, count=10)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                logger.warning(
                    "subscribe_simple xread failed (attempt %d): %s",
                    attempt + 1, exc,
                )
                await _backoff_sleep(attempt)
                attempt += 1
                try:
                    await self._reconnect()
                except Exception as re_exc:
                    if not _is_transient(re_exc):
                        raise
                    logger.warning(
                        "subscribe_simple reconnect failed (attempt %d): %s",
                        attempt, re_exc,
                    )
                continue

            attempt = 0  # reset on success
            if not results:
                continue

            for stream_key, messages in results:
                for msg_id, data in messages:
                    streams[stream_key] = msg_id
                    try:
                        yield Envelope.from_json(data["data"])
                    except Exception:
                        logger.warning(
                            "Failed to parse message %s from %s",
                            msg_id, stream_key,
                        )

    async def subscribe_group(
        self,
        channel: str,
        group: str,
        consumer: str,
        min_idle_time: int = 30_000,
    ) -> AsyncIterator[tuple[str, Envelope]]:
        """Yield (msg_id, envelope) using consumer groups for load-balanced reads.

        Automatically creates the group if it doesn't exist.
        On startup, reclaims pending messages from dead consumers via XAUTOCLAIM
        before reading new messages (prevents stranded work after crashes).
        On transient connection errors the subscriber reconnects with exponential
        backoff and resumes reading new messages with '>'.
        """
        stream_key = f"stream:{channel}"
        await self._ensure_connected()
        try:
            await self.redis.xgroup_create(
                stream_key, group, id="0", mkstream=True,
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        # Reclaim pending messages from dead consumers
        try:
            start_id = "0-0"
            while True:
                result = await self.redis.xautoclaim(
                    stream_key, group, consumer,
                    min_idle_time=min_idle_time, start_id=start_id, count=10,
                )
                next_id, messages, _deleted = result
                for msg_id, data in messages:
                    if data:
                        try:
                            envelope = Envelope.from_json(data["data"])
                            logger.info("Reclaimed pending message %s on %s", msg_id, channel)
                            yield msg_id, envelope
                        except Exception:
                            logger.warning("Failed to parse reclaimed message %s", msg_id)
                            await self.redis.xack(stream_key, group, msg_id)
                if next_id == "0-0" or not messages:
                    break
                start_id = next_id
        except Exception:
            logger.debug("XAUTOCLAIM not available or failed on %s, skipping reclaim", channel, exc_info=True)

        attempt = 0
        while True:
            try:
                await self._ensure_connected()
                results = await self.redis.xreadgroup(
                    group, consumer, {stream_key: ">"}, block=2000, count=1,
                )
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                logger.warning(
                    "subscribe_group xreadgroup failed (attempt %d): %s",
                    attempt + 1, exc,
                )
                await _backoff_sleep(attempt)
                attempt += 1
                try:
                    await self._reconnect()
                except Exception as re_exc:
                    if not _is_transient(re_exc):
                        raise
                    logger.warning(
                        "subscribe_group reconnect failed (attempt %d): %s",
                        attempt, re_exc,
                    )
                continue

            attempt = 0  # reset on success
            if not results:
                continue

            for _stream, messages in results:
                for msg_id, data in messages:
                    try:
                        envelope = Envelope.from_json(data["data"])
                        yield msg_id, envelope
                    except Exception:
                        logger.warning("Failed to parse message %s", msg_id)
                        await self.redis.xack(stream_key, group, msg_id)

    async def ack(self, channel: str, group: str, msg_id: str) -> None:
        await self._ensure_connected()
        await self.redis.xack(f"stream:{channel}", group, msg_id)

    async def get_history(
        self, channel: str, count: int = 100,
    ) -> list[Envelope]:
        """Read the last `count` messages from a stream."""
        stream_key = f"stream:{channel}"
        await self._ensure_connected()
        results = await self.redis.xrevrange(stream_key, count=count)
        envelopes = []
        for _msg_id, data in reversed(results):
            try:
                envelopes.append(Envelope.from_json(data["data"]))
            except Exception:
                continue
        return envelopes

    async def wait_for_message(
        self, channel: str, timeout_ms: int, last_id: str = "$",
    ) -> Envelope | None:
        """Block until a single new message arrives on a channel, or timeout.

        Uses XREAD with blocking — no polling. On transient connection errors
        the method reconnects and retries, but tracks elapsed time against a
        monotonic deadline so that backoff does not silently extend the wait.
        Returns the envelope or None if the timeout expired.
        """
        stream_key = f"stream:{channel}"
        deadline = time.monotonic() + timeout_ms / 1000.0
        attempt = 0

        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return None
            remaining_ms = max(int(remaining_s * 1000), 1)

            try:
                await self._ensure_connected()
                results = await self.redis.xread(
                    {stream_key: last_id}, block=remaining_ms, count=1,
                )
                if not results:
                    return None

                for _stream, messages in results:
                    for _msg_id, data in messages:
                        try:
                            return Envelope.from_json(data["data"])
                        except Exception:
                            return None
                return None
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0:
                    return None
                # Cap the backoff delay at the remaining time budget
                delay = min(
                    _BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP,
                )
                jittered = random.uniform(0, delay)  # noqa: S311
                sleep_time = min(jittered, remaining_s)
                logger.warning(
                    "wait_for_message() connection error, retrying in %.1fs: %s",
                    sleep_time, exc,
                )
                await asyncio.sleep(sleep_time)
                attempt += 1
                try:
                    await self._reconnect()
                except Exception as re_exc:
                    if not _is_transient(re_exc):
                        raise
                    logger.warning(
                        "wait_for_message reconnect failed (attempt %d): %s",
                        attempt, re_exc,
                    )
