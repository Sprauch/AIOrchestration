"""Per-thread workflow guard — prevents unbounded review/change cycles.

Tracks failed review-cycle counts per thread_id in Redis. Blocks new
review-request or review-result messages after max_change_rounds failed
cycles (changes_requested) on the same thread. Approved reviews don't
count toward the limit.

Uses a Lua script for atomic check-and-increment to prevent race conditions
where two concurrent changes_requested messages both pass the limit check.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

THREAD_CYCLES_KEY = "orchestrator:thread_cycles"

# Message types subject to cycle checks
CYCLE_TYPES = {"review_request", "review_result"}

# Lua script: atomic read + check + conditional increment
# Returns -1 if blocked, else the current count (before increment)
_LUA_CHECK_AND_INCREMENT = """
local key = KEYS[1]
local field = ARGV[1]
local max = tonumber(ARGV[2])
local incr = tonumber(ARGV[3])
local count = tonumber(redis.call('hget', key, field) or '0')
if count >= max then return -1 end
if incr == 1 then redis.call('hincrby', key, field, 1) end
return count
"""


class ThreadGuard:
    """Redis-backed per-thread cycle counter with atomic Lua operations."""

    def __init__(self, redis, max_rounds: int = 3):
        self._redis = redis
        self.max_rounds = max_rounds

    async def check_and_increment(
        self,
        thread_id: str,
        message_type: str,
        decision: str | None = None,
    ) -> bool:
        """Check if this thread is within cycle limits.

        Increments the counter only for review_result with decision=changes_requested.
        Approved reviews don't count — the cycle limit tracks failed attempts only.

        Returns True if allowed, False if blocked.
        Uses a single atomic Lua eval to prevent race conditions.
        """
        if message_type not in CYCLE_TYPES:
            return True

        field = f"{thread_id}:cycles"
        should_increment = 1 if (message_type == "review_result" and decision == "changes_requested") else 0

        try:
            result = await self._redis.eval(
                _LUA_CHECK_AND_INCREMENT, 1,
                THREAD_CYCLES_KEY, field, str(self.max_rounds), str(should_increment),
            )
            if result == -1:
                logger.warning(
                    "Thread %s blocked: review cycle limit reached (max %d)",
                    thread_id[:8], self.max_rounds,
                )
                return False
            return True
        except Exception:
            logger.error(
                "Thread %s: Redis eval failed during cycle check — denying",
                thread_id[:8], exc_info=True,
            )
            return False  # fail closed

    async def get_cycle_count(self, thread_id: str) -> int:
        """Read the current failed-cycle count for a thread."""
        try:
            current = await self._redis.hget(THREAD_CYCLES_KEY, f"{thread_id}:cycles")
            return int(current) if current else 0
        except Exception:
            return 0
