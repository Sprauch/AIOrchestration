"""Pipeline auditor — fact-based system assessment from Redis state.

Reports concrete, verifiable facts about the pipeline. Avoids interpretive
judgments about whether normal workflow patterns are "problems." Each finding
is an invariant violation or a measurable state, not a heuristic guess.

Findings are classified as:
  - fact: a concrete state that requires or may require action
  - observation: a measurable pipeline metric, no judgment attached

Usage:
    findings = await audit_pipeline("redis://localhost:6379/0")
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

import redis.asyncio as aioredis

from agents.core.message import Envelope
from agents.core.thread_guard import THREAD_CYCLES_KEY

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    severity: str       # "critical", "warning", "info"
    kind: str           # "fact" or "observation"
    category: str       # "cycles", "pr", "agent", "gate", "throughput"
    summary: str        # one-line, states what IS, not what it means
    detail: str         # actionable next step or empty
    thread_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


async def audit_pipeline(redis_url: str) -> list[Finding]:
    """Query pipeline state and return fact-based findings."""
    r = aioredis.from_url(redis_url, decode_responses=True)
    findings: list[Finding] = []

    try:
        await _check_exhausted_cycles(r, findings)
        await _check_pending_gates(r, findings)
        await _check_failed_prs(r, findings)
        await _check_busy_stale_agents(r, findings)
        await _check_consumed_no_output(r, findings)
        await _check_throughput(r, findings)
    except Exception:
        logger.exception("Auditor failed")
        findings.append(Finding(
            "critical", "fact", "health",
            "Auditor error — could not complete assessment",
            "Check Redis connectivity and logs.",
        ))
    finally:
        await r.aclose()

    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return findings


async def _check_exhausted_cycles(r: aioredis.Redis, findings: list[Finding]) -> None:
    """Fact: threads that have hit the review cycle limit are blocked."""
    try:
        raw = await r.hgetall(THREAD_CYCLES_KEY)
    except Exception:
        return

    for field, val in raw.items():
        tid = field.replace(":cycles", "")
        count = int(val)
        if count >= 3:
            findings.append(Finding(
                "critical", "fact", "cycles",
                f"Thread {tid[:8]} blocked — exhausted {count} review cycles",
                "Use 'Reset Cycles' to retry or investigate blocking issues in thread detail.",
                thread_id=tid,
            ))


async def _check_pending_gates(r: aioredis.Redis, findings: list[Finding]) -> None:
    """Fact: user gates awaiting response block agent progress.

    Checks each gate's dedicated response channel (gate-responses:{gate_id})
    instead of scanning a windowed system stream — exact, not approximate.
    """
    try:
        gates = await r.xrange("stream:user-gates")
    except Exception:
        return

    pending = 0
    for _mid, data in gates:
        try:
            env = Envelope.from_json(data["data"])
            # Each gate has a per-gate response channel. If it has messages,
            # the gate was responded to (approved or denied).
            response_count = await r.xlen(f"stream:gate-responses:{env.id}")
            if response_count == 0:
                pending += 1
        except Exception:
            continue

    if pending:
        findings.append(Finding(
            "warning", "fact", "gate",
            f"{pending} user gate(s) awaiting response",
            "Approve or deny in the Exceptions tab.",
        ))


async def _check_failed_prs(r: aioredis.Redis, findings: list[Finding]) -> None:
    """Fact: PR creation failures are concrete and actionable."""
    try:
        prs = await r.hgetall("orchestrator:created_prs")
    except Exception:
        return

    failed, created = [], 0
    for tid, val in prs.items():
        if "failed" in val.lower():
            parts = val.split("|", 2)
            detail = parts[2] if len(parts) > 2 else ""
            failed.append((tid, detail))
        elif val.startswith("http"):
            created += 1

    if failed:
        findings.append(Finding(
            "critical", "fact", "pr",
            f"{len(failed)} PR(s) failed to create",
            "Failed threads: " + ", ".join(tid[:8] for tid, _ in failed) +
            ". Use 'Retry' in Exceptions tab or check gh CLI auth.",
        ))

    if created:
        findings.append(Finding(
            "info", "fact", "pr",
            f"{created} PR(s) created successfully",
            "",
        ))


async def _check_busy_stale_agents(r: aioredis.Redis, findings: list[Finding]) -> None:
    """Fact: an agent marked 'busy' with a stale heartbeat is stuck.

    Only flags agents whose status is 'busy' but heartbeat is old — this is a
    concrete invariant violation, not a heuristic. Idle agents with old
    heartbeats are normal (waiting for messages).
    """
    now = time.time()

    try:
        keys = []
        async for key in r.scan_iter("agent:*:status"):
            keys.append(key)
    except Exception:
        return

    active_count = 0
    for key in keys:
        agent_id = key.replace("agent:", "").replace(":status", "")
        status = await r.get(key) or ""
        if status in ("stopped", "paused"):
            continue
        active_count += 1

        if status != "busy":
            continue

        hb = await r.get(f"agent:{agent_id}:heartbeat")
        if not hb:
            continue
        age = int(now - float(hb))
        # busy + heartbeat older than 5 minutes = stuck
        if age > 300:
            findings.append(Finding(
                "warning", "fact", "agent",
                f"Agent {agent_id} marked busy but heartbeat is {age}s old",
                "Agent may be hung on a CLI call. Check logs or consider restarting.",
            ))

    if active_count:
        findings.append(Finding(
            "info", "observation", "agent",
            f"{active_count} agent(s) registered",
            "",
        ))


async def _check_consumed_no_output(r: aioredis.Redis, findings: list[Finding]) -> None:
    """Fact: a consumer group ACKed all input but produced no output.

    This checks whether a stage consumed messages and ACKed them but the
    downstream stream is empty. Only reports if input > 0, output == 0,
    and pending == 0 — a concrete state, not a timing heuristic.
    """
    steps = [
        ("proposals", "reviews", "Architect consumed proposals but produced no reviews"),
        ("tasks", "review-requests", "Developer consumed tasks but produced no review requests"),
        ("review-requests", "review-results", "Reviewer consumed requests but produced no results"),
    ]

    for in_stream, out_stream, description in steps:
        try:
            in_count = await r.xlen(f"stream:{in_stream}")
            out_count = await r.xlen(f"stream:{out_stream}")
        except Exception:
            continue

        if in_count == 0 or out_count > 0:
            continue

        all_acked = True
        try:
            groups = await r.xinfo_groups(f"stream:{in_stream}")
            for g in groups:
                if g.get("pending", 0) > 0:
                    all_acked = False
                    break
        except Exception:
            continue

        if all_acked:
            findings.append(Finding(
                "warning", "fact", "throughput",
                description,
                f"{in_stream} has {in_count} messages, all ACKed, but {out_stream} is empty.",
            ))


async def _check_throughput(r: aioredis.Redis, findings: list[Finding]) -> None:
    """Observation: pipeline stream counts — no judgment, just numbers."""
    counts = {}
    for stream in ("proposals", "reviews", "tasks", "review-requests", "review-results"):
        try:
            counts[stream] = await r.xlen(f"stream:{stream}")
        except Exception:
            counts[stream] = 0

    # Count review decisions for factual reporting
    approved, changes_requested = 0, 0
    try:
        results = await r.xrevrange("stream:review-results", count=50)
        for _mid, data in results:
            try:
                env = Envelope.from_json(data["data"])
                decision = env.payload.get("decision", "")
                if decision == "approved":
                    approved += 1
                elif decision == "changes_requested":
                    changes_requested += 1
            except Exception:
                continue
    except Exception:
        pass

    parts = [
        f"{counts['proposals']} proposals",
        f"{counts['tasks']} tasks",
        f"{counts['review-requests']} in review",
    ]
    if approved or changes_requested:
        parts.append(f"{approved} approved / {changes_requested} changes requested")

    findings.append(Finding(
        "info", "observation", "throughput",
        "Pipeline: " + ", ".join(parts),
        "",
    ))
