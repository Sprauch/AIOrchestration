"""Tests for pipeline auditor — fact-based findings from Redis state."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from agents.core.auditor import (
    Finding,
    _check_busy_stale_agents,
    _check_consumed_no_output,
    _check_exhausted_cycles,
    _check_failed_prs,
    _check_pending_gates,
    _check_throughput,
)
from agents.core.message import Envelope, MessageType
from agents.core.thread_guard import THREAD_CYCLES_KEY


class FakeRedis:
    """Minimal async Redis mock for auditor tests."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self._groups: dict[str, list[dict]] = {}

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def xrevrange(self, stream, count=100):
        return list(reversed(self.streams.get(stream, [])))[:count]

    async def xrange(self, stream):
        return self.streams.get(stream, [])

    async def xlen(self, stream):
        return len(self.streams.get(stream, []))

    async def xinfo_groups(self, stream):
        return self._groups.get(stream, [])

    async def get(self, key):
        return self.strings.get(key)

    def scan_iter(self, pattern):
        import fnmatch
        return _async_iter([k for k in self.strings if fnmatch.fnmatch(k, pattern)])

    def add_stream_message(self, stream: str, envelope: Envelope) -> None:
        if stream not in self.streams:
            self.streams[stream] = []
        self.streams[stream].append(("fake-id", {"data": envelope.to_json()}))


async def _async_iter(items):
    for item in items:
        yield item


def _env(message_type=MessageType.SYSTEM, payload=None, thread_id="t1", sender_id="test"):
    return Envelope(
        sender_id=sender_id,
        sender_role="system",
        message_type=message_type,
        payload=payload or {},
        thread_id=thread_id,
    )


# ── Exhausted cycles ──


@pytest.mark.asyncio
async def test_exhausted_cycles_found():
    r = FakeRedis()
    r.hashes[THREAD_CYCLES_KEY] = {"abc123:cycles": "3"}
    findings: list[Finding] = []
    await _check_exhausted_cycles(r, findings)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].kind == "fact"
    assert "abc123" in findings[0].summary
    assert findings[0].thread_id == "abc123"


@pytest.mark.asyncio
async def test_exhausted_cycles_below_threshold():
    r = FakeRedis()
    r.hashes[THREAD_CYCLES_KEY] = {"abc123:cycles": "2"}
    findings: list[Finding] = []
    await _check_exhausted_cycles(r, findings)
    assert len(findings) == 0


@pytest.mark.asyncio
async def test_exhausted_cycles_empty():
    r = FakeRedis()
    findings: list[Finding] = []
    await _check_exhausted_cycles(r, findings)
    assert len(findings) == 0


# ── Pending gates ──


@pytest.mark.asyncio
async def test_pending_gate_found():
    r = FakeRedis()
    gate = _env(MessageType.HUMAN_GATE, {"action": "create_pr"})
    r.add_stream_message("stream:human-gates", gate)
    # No response in system stream
    findings: list[Finding] = []
    await _check_pending_gates(r, findings)
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert "gate" in findings[0].summary.lower()


@pytest.mark.asyncio
async def test_gate_already_responded():
    r = FakeRedis()
    gate = _env(MessageType.HUMAN_GATE, {"action": "create_pr"})
    r.add_stream_message("stream:human-gates", gate)
    # Response exists on the per-gate response channel
    response = _env(MessageType.SYSTEM, {"action": "approval_granted", "gate_id": gate.id})
    r.add_stream_message(f"stream:gate-responses:{gate.id}", response)
    findings: list[Finding] = []
    await _check_pending_gates(r, findings)
    assert len(findings) == 0


# ── Failed PRs ──


@pytest.mark.asyncio
async def test_failed_pr_found():
    r = FakeRedis()
    r.hashes["orchestrator:created_prs"] = {
        "thread1": "failed|2026-01-01T00:00:00Z|auth error",
    }
    findings: list[Finding] = []
    await _check_failed_prs(r, findings)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].kind == "fact"


@pytest.mark.asyncio
async def test_created_pr_is_info():
    r = FakeRedis()
    r.hashes["orchestrator:created_prs"] = {
        "thread1": "https://github.com/org/repo/pull/1|2026-01-01T00:00:00Z|",
    }
    findings: list[Finding] = []
    await _check_failed_prs(r, findings)
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert "1 PR(s) created" in findings[0].summary


@pytest.mark.asyncio
async def test_no_prs():
    r = FakeRedis()
    findings: list[Finding] = []
    await _check_failed_prs(r, findings)
    assert len(findings) == 0


# ── Busy stale agents ──


@pytest.mark.asyncio
async def test_busy_stale_agent():
    r = FakeRedis()
    r.strings["agent:dev-1:status"] = "busy"
    r.strings["agent:dev-1:heartbeat"] = str(time.time() - 400)
    findings: list[Finding] = []
    await _check_busy_stale_agents(r, findings)
    facts = [f for f in findings if f.kind == "fact"]
    assert len(facts) == 1
    assert "dev-1" in facts[0].summary
    assert facts[0].severity == "warning"


@pytest.mark.asyncio
async def test_busy_fresh_agent_not_flagged():
    r = FakeRedis()
    r.strings["agent:dev-1:status"] = "busy"
    r.strings["agent:dev-1:heartbeat"] = str(time.time() - 10)
    findings: list[Finding] = []
    await _check_busy_stale_agents(r, findings)
    facts = [f for f in findings if f.kind == "fact"]
    assert len(facts) == 0


@pytest.mark.asyncio
async def test_active_idle_agent_not_flagged():
    r = FakeRedis()
    r.strings["agent:dev-1:status"] = "active"
    r.strings["agent:dev-1:heartbeat"] = str(time.time() - 600)
    findings: list[Finding] = []
    await _check_busy_stale_agents(r, findings)
    facts = [f for f in findings if f.kind == "fact"]
    assert len(facts) == 0  # active + old heartbeat = just waiting, not stuck


# ── Consumed no output ──


@pytest.mark.asyncio
async def test_consumed_no_output_detected():
    r = FakeRedis()
    r.add_stream_message("stream:review-requests", _env(MessageType.REVIEW_REQUEST))
    r._groups["stream:review-requests"] = [{"name": "reviewer-group", "pending": 0}]
    # review-results is empty
    findings: list[Finding] = []
    await _check_consumed_no_output(r, findings)
    assert len(findings) == 1
    assert findings[0].kind == "fact"
    assert "no results" in findings[0].summary.lower()


@pytest.mark.asyncio
async def test_consumed_with_output_ok():
    r = FakeRedis()
    r.add_stream_message("stream:review-requests", _env(MessageType.REVIEW_REQUEST))
    r.add_stream_message("stream:review-results", _env(MessageType.REVIEW_RESULT, {"decision": "approved"}))
    r._groups["stream:review-requests"] = [{"name": "reviewer-group", "pending": 0}]
    findings: list[Finding] = []
    await _check_consumed_no_output(r, findings)
    assert len(findings) == 0


@pytest.mark.asyncio
async def test_pending_messages_not_flagged():
    r = FakeRedis()
    r.add_stream_message("stream:review-requests", _env(MessageType.REVIEW_REQUEST))
    r._groups["stream:review-requests"] = [{"name": "reviewer-group", "pending": 1}]
    findings: list[Finding] = []
    await _check_consumed_no_output(r, findings)
    assert len(findings) == 0  # still being processed


# ── Throughput ──


@pytest.mark.asyncio
async def test_throughput_observation():
    r = FakeRedis()
    for _ in range(3):
        r.add_stream_message("stream:proposals", _env(MessageType.PROPOSAL))
    findings: list[Finding] = []
    await _check_throughput(r, findings)
    assert len(findings) == 1
    assert findings[0].kind == "observation"
    assert "3 proposals" in findings[0].summary


@pytest.mark.asyncio
async def test_throughput_includes_review_decisions():
    r = FakeRedis()
    r.add_stream_message("stream:proposals", _env(MessageType.PROPOSAL))
    r.add_stream_message("stream:review-results", _env(MessageType.REVIEW_RESULT, {"decision": "approved"}))
    r.add_stream_message("stream:review-results", _env(MessageType.REVIEW_RESULT, {"decision": "changes_requested"}))
    findings: list[Finding] = []
    await _check_throughput(r, findings)
    assert len(findings) == 1
    assert "1 approved" in findings[0].summary
    assert "1 changes requested" in findings[0].summary


# ── Finding serialization ──


def test_finding_to_dict():
    f = Finding("warning", "fact", "cycles", "Thread abc blocked", "Reset it.", thread_id="abc")
    d = f.to_dict()
    assert d["severity"] == "warning"
    assert d["kind"] == "fact"
    assert d["thread_id"] == "abc"
