"""Shared read model — queries Redis for system state.

Used by both the TUI monitor and the CLI status command.
Returns plain data structures, no rendering.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

from agents.core.metrics import METRICS_KEY

ALL_STREAMS = [
    "system", "proposals", "reviews", "tasks",
    "review-requests", "review-results", "progress", "user-gates",
]


@dataclass
class AgentState:
    agent_id: str
    status: str
    heartbeat: float | None  # unix timestamp, or None
    current_task: str
    paused: bool = False

    @property
    def heartbeat_age(self) -> float | None:
        if self.heartbeat is None:
            return None
        return time.time() - self.heartbeat


@dataclass
class StreamInfo:
    name: str
    count: int
    groups: list[ConsumerGroupInfo] = field(default_factory=list)


@dataclass
class ConsumerGroupInfo:
    name: str
    consumers: int
    pending: int


@dataclass
class SystemSnapshot:
    """Complete point-in-time snapshot of the orchestrator's state."""

    agents: list[AgentState]
    streams: list[StreamInfo]
    metrics: dict[str, int]
    orchestrator_heartbeat: float | None  # unix timestamp


async def load_snapshot(redis_url: str) -> SystemSnapshot | None:
    """Load a full system snapshot from Redis. Returns None if Redis is unreachable."""
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        await r.ping()
    except Exception:
        return None

    try:
        return await _query_snapshot(r)
    finally:
        await r.aclose()


async def load_snapshot_from_connection(r: aioredis.Redis) -> SystemSnapshot:
    """Load a snapshot using an existing Redis connection."""
    return await _query_snapshot(r)


async def _query_snapshot(r: aioredis.Redis) -> SystemSnapshot:
    # Agents
    agents = []
    async for key in r.scan_iter("agent:*:status"):
        agent_id = key.split(":")[1]
        status = await r.get(key) or "unknown"
        hb_raw = await r.get(f"agent:{agent_id}:heartbeat")
        task = await r.get(f"agent:{agent_id}:current_task") or ""
        paused_raw = await r.get(f"agent:{agent_id}:paused")
        agents.append(AgentState(
            agent_id=agent_id,
            status=status,
            heartbeat=float(hb_raw) if hb_raw else None,
            current_task=task,
            paused=paused_raw == "1",
        ))
    agents.sort(key=lambda a: a.agent_id)

    # Streams
    streams = []
    for name in ALL_STREAMS:
        stream_key = f"stream:{name}"
        try:
            count = await r.xlen(stream_key)
        except Exception:
            logger.debug("xlen lookup failed for %s", stream_key, exc_info=True)
            count = 0

        groups = []
        try:
            raw_groups = await r.xinfo_groups(stream_key)
            for g in raw_groups:
                groups.append(ConsumerGroupInfo(
                    name=g.get("name", "?"),
                    consumers=g.get("consumers", 0),
                    pending=g.get("pending", 0),
                ))
        except Exception:
            logger.debug("xinfo_groups lookup failed for %s", stream_key, exc_info=True)

        streams.append(StreamInfo(name=name, count=count, groups=groups))

    # Metrics
    try:
        raw = await r.hgetall(METRICS_KEY)
        metrics = {k: int(v) for k, v in raw.items()}
    except Exception:
        logger.debug("metrics hgetall failed", exc_info=True)
        metrics = {}

    # Orchestrator heartbeat
    try:
        hb_raw = await r.get("orchestrator:heartbeat")
        orch_hb = float(hb_raw) if hb_raw else None
    except Exception:
        logger.debug("orchestrator heartbeat read failed", exc_info=True)
        orch_hb = None

    return SystemSnapshot(
        agents=agents,
        streams=streams,
        metrics=metrics,
        orchestrator_heartbeat=orch_hb,
    )


# ── Pipeline phase derivation ─────────────────────────────

# THE CHAIN, IN EXECUTION ORDER, INCLUDING ROLES THAT DO NOT EXIST YET.
#
# Listing the full intended chain here rather than only what is wired up means a new role
# appears in the right place the moment it is added, instead of needing the display to be
# changed as well. A phase with no agent behind it simply never becomes active.
#
# DATA ENGINEER IS DELIBERATELY ABSENT, and that is a statement about the shape of the
# chain rather than an oversight: it is A FORM OF DEVELOPER, not a stage of its own. It
# differs in the context it is given - the data, the ETL - not in where it sits in the
# pipeline. Future specialisations are expected on the same basis, and they all collapse
# onto the developer step, via DEVELOPER_VARIANTS below.
PIPELINE_PHASES = [
    "pm",
    "product_designer",
    "architect",
    "developer",
    "tester",
    "devops",
    "tech_writer",
    "retrospective",
]

# Roles that ARE developers, shown at the developer step rather than beside it.
DEVELOPER_VARIANTS = {"data_engineer"}

PHASE_LABELS = {
    "pm": "PM proposing",
    "product_designer": "Designer reviewing",
    "architect": "Architect reviewing",
    "developer": "Developer implementing",
    "tester": "Tester verifying",
    "devops": "DevOps deploying",
    "tech_writer": "Tech Writer recording",
    "retrospective": "Retro reviewing the work",
}

# What the header shows. Short, because eight of them share one strip.
PHASE_SHORT = {
    "pm": "PM",
    "product_designer": "Designer",
    "architect": "Architect",
    "developer": "Developer",
    "tester": "Tester",
    "devops": "DevOps",
    "tech_writer": "TechWriter",
    "retrospective": "Retro",
}

# The reviewer predates the tester and does the same job at the same point in the chain.
# Mapped rather than renamed, so an existing deployment keeps working.
ROLE_TO_PHASE = {"reviewer": "tester", **{v: "developer" for v in DEVELOPER_VARIANTS}}


def derive_current_phase(
    agents: list[AgentState],
    backpressure: dict[str, dict] | None = None,
) -> dict:
    """Derive the current pipeline phase from agent and backpressure state.

    Returns a dict with:
      - current_phase: str | None  (one of PIPELINE_PHASES or None for idle)
      - phases: list of {name, label, state} where state is
        "completed", "active", "pending", or "inactive"
    """
    # Determine which roles are actively doing work
    active_roles: set[str] = set()

    # Map short-form prefixes to canonical role names
    _short_to_role = {"arch": "architect", "tl": "architect", "pd": "product_designer", "dev": "developer", "rev": "reviewer"}

    for agent in agents:
        prefix = agent.agent_id.split("-")[0]  # "pm-1" -> "pm", "dev-1" -> "dev"
        r = _short_to_role.get(prefix, prefix)
        r = ROLE_TO_PHASE.get(r, r)  # a variant shows at the step it belongs to
        if r in PIPELINE_PHASES and agent.status == "busy":
            active_roles.add(r)

    # Backpressure stages map to roles that consume from those queues:
    # designs -> product designer, proposals -> architect, tasks -> developer, reviews -> reviewer
    _bp_to_role = {"designs": "product_designer", "proposals": "architect", "tasks": "developer", "reviews": "reviewer"}
    if backpressure:
        for stage, info in backpressure.items():
            if isinstance(info, dict) and info.get("active", 0) > 0:
                mapped_role = _bp_to_role.get(stage)
                if mapped_role:
                    active_roles.add(ROLE_TO_PHASE.get(mapped_role, mapped_role))

    if not active_roles:
        return {
            "current_phase": None,
            "phases": [
                {"name": p, "label": PHASE_LABELS[p], "short": PHASE_SHORT[p], "state": "inactive"}
                for p in PIPELINE_PHASES
            ],
        }

    # Highest-precedence active phase (furthest along the pipeline)
    current = None
    for phase in reversed(PIPELINE_PHASES):
        if phase in active_roles:
            current = phase
            break

    current_idx = PIPELINE_PHASES.index(current)
    phases = []
    for i, p in enumerate(PIPELINE_PHASES):
        if p in active_roles:
            state = "active"
        elif i < current_idx:
            state = "completed"
        else:
            state = "pending"
        phases.append({"name": p, "label": PHASE_LABELS[p], "short": PHASE_SHORT[p], "state": state})

    return {"current_phase": current, "phases": phases}
