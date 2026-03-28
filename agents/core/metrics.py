"""Lightweight metrics — counters stored in a Redis hash.

All metrics live under a single Redis hash key `orchestrator:metrics`.
Counters are incremented atomically via HINCRBY. This is low-overhead,
requires no additional infrastructure, and is readable via `agent-orchestrator status`
or `redis-cli HGETALL orchestrator:metrics`.

Metric names follow the pattern: {category}:{detail}
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

METRICS_KEY = "orchestrator:metrics"


class Metrics:
    """Accumulates counters in a Redis hash."""

    def __init__(self, redis):
        self._redis = redis

    async def increment(self, name: str, amount: int = 1) -> None:
        """Increment a named counter."""
        try:
            await self._redis.hincrby(METRICS_KEY, name, amount)
        except Exception:
            logger.debug("Failed to increment metric %s", name, exc_info=True)

    async def get_all(self) -> dict[str, int]:
        """Read all metrics as a dict."""
        try:
            raw = await self._redis.hgetall(METRICS_KEY)
            return {k: int(v) for k, v in raw.items()}
        except Exception:
            logger.debug("metrics get_all failed", exc_info=True)
            return {}
