"""Correlation-ID logging context.

Provides contextvars that carry thread_id and agent_id through async call
stacks, plus a logging.Filter that injects them into every LogRecord so the
formatter can include them automatically.

Usage in format strings:  %(thread_ctx)s  %(agent_ctx)s
Outside envelope processing both default to "-".
"""

from __future__ import annotations

import contextvars
import logging

# ── Context variables ─────────────────────────────────────
_thread_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("thread_ctx", default="-")
_agent_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("agent_ctx", default="-")


def set_correlation(thread_id: str, agent_id: str) -> None:
    """Set the current correlation IDs (8-char thread prefix stored)."""
    _thread_ctx.set(thread_id[:8] if thread_id else "-")
    _agent_ctx.set(agent_id or "-")


def clear_correlation() -> None:
    """Reset correlation IDs to defaults."""
    _thread_ctx.set("-")
    _agent_ctx.set("-")


def get_thread_ctx() -> str:
    return _thread_ctx.get()


def get_agent_ctx() -> str:
    return _agent_ctx.get()


# ── Logging filter ────────────────────────────────────────
class CorrelationFilter(logging.Filter):
    """Injects thread_ctx and agent_ctx into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.thread_ctx = _thread_ctx.get()  # type: ignore[attr-defined]
        record.agent_ctx = _agent_ctx.get()  # type: ignore[attr-defined]
        return True
