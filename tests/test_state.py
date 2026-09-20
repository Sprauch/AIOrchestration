"""Tests for the shared read model (agents/core/state.py)."""

import json
from dataclasses import asdict

from agents.core.state import AgentState, ConsumerGroupInfo, StreamInfo, SystemSnapshot, derive_current_phase


def test_agent_state_heartbeat_age():
    import time
    a = AgentState(
        agent_id="pm-1", status="active",
        heartbeat=time.time() - 30, current_task="analyzing",
    )
    assert a.heartbeat_age is not None
    assert 29 <= a.heartbeat_age <= 32


def test_agent_state_no_heartbeat():
    a = AgentState(agent_id="pm-1", status="unknown", heartbeat=None, current_task="")
    assert a.heartbeat_age is None


def test_snapshot_serializable():
    """SystemSnapshot must be JSON-serializable via dataclasses.asdict for --json."""
    snapshot = SystemSnapshot(
        agents=[
            AgentState("pm-1", "active", 1700000000.0, "analyzing"),
            AgentState("dev-1", "busy", None, ""),
        ],
        streams=[
            StreamInfo("proposals", 5, [ConsumerGroupInfo("dev-group", 2, 1)]),
            StreamInfo("tasks", 0, []),
        ],
        metrics={"messages:total": 42, "errors:total": 1},
        orchestrator_heartbeat=1700000010.0,
    )
    data = asdict(snapshot)
    text = json.dumps(data, default=str)
    parsed = json.loads(text)

    assert len(parsed["agents"]) == 2
    assert parsed["agents"][0]["agent_id"] == "pm-1"
    assert parsed["streams"][0]["count"] == 5
    assert parsed["streams"][0]["groups"][0]["name"] == "dev-group"
    assert parsed["metrics"]["messages:total"] == 42
    assert parsed["orchestrator_heartbeat"] == 1700000010.0


def test_snapshot_empty():
    snapshot = SystemSnapshot(
        agents=[], streams=[], metrics={}, orchestrator_heartbeat=None,
    )
    data = asdict(snapshot)
    text = json.dumps(data, default=str)
    parsed = json.loads(text)
    assert parsed["agents"] == []
    assert parsed["orchestrator_heartbeat"] is None


# ── derive_current_phase tests ──────────────────────────────


def test_phase_idle_no_agents():
    result = derive_current_phase([], None)
    assert result["current_phase"] is None
    assert all(p["state"] == "inactive" for p in result["phases"])
    # Five phases, not the upstream four: product_designer runs between pm and
    # architect. Named rather than counted so a change says which phase moved.
    assert [p["name"] for p in result["phases"]] == [
        "pm", "product_designer", "architect", "developer", "reviewer",
    ]


def test_phase_idle_agents_active_not_busy():
    agents = [AgentState("pm-1", "active", None, "")]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] is None


def test_phase_pm_busy():
    agents = [AgentState("pm-1", "busy", None, "analyzing")]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] == "pm"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["pm"] == "active"
    assert phases["architect"] == "pending"
    assert phases["developer"] == "pending"
    assert phases["reviewer"] == "pending"


def test_phase_developer_from_backpressure():
    agents = [AgentState("pm-1", "active", None, "")]
    bp = {"proposals": {"active": 0, "limit": 3}, "tasks": {"active": 2, "limit": 3}, "reviews": {"active": 0, "limit": 5}}
    result = derive_current_phase(agents, bp)
    assert result["current_phase"] == "developer"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["pm"] == "completed"
    assert phases["architect"] == "completed"
    assert phases["developer"] == "active"
    assert phases["reviewer"] == "pending"


def test_phase_multi_active_highest_wins():
    """When multiple phases are active, the furthest-along takes precedence."""
    agents = [
        AgentState("pm-1", "busy", None, ""),
        AgentState("developer-1", "busy", None, ""),
        AgentState("reviewer-1", "busy", None, ""),
    ]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] == "reviewer"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["pm"] == "active"
    assert phases["architect"] == "completed"
    assert phases["developer"] == "active"
    assert phases["reviewer"] == "active"


def test_phase_reviewer_from_backpressure():
    agents = []
    bp = {"proposals": {"active": 0, "limit": 3}, "tasks": {"active": 0, "limit": 3}, "reviews": {"active": 1, "limit": 5}}
    result = derive_current_phase(agents, bp)
    assert result["current_phase"] == "reviewer"


def test_phase_short_id_arch():
    """Short-form 'arch-1' maps to architect phase."""
    agents = [AgentState("arch-1", "busy", None, "designing")]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] == "architect"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["architect"] == "active"


def test_phase_short_id_dev():
    """Short-form 'dev-1' maps to developer phase."""
    agents = [AgentState("dev-1", "busy", None, "coding")]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] == "developer"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["developer"] == "active"


def test_phase_short_id_rev():
    """Short-form 'rev-1' maps to reviewer phase."""
    agents = [AgentState("rev-1", "busy", None, "reviewing")]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] == "reviewer"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["reviewer"] == "active"


def test_phase_mixed_short_and_long_ids():
    """Mix of short and long-form IDs both contribute to active roles."""
    agents = [
        AgentState("dev-1", "busy", None, ""),
        AgentState("reviewer-1", "busy", None, ""),
    ]
    result = derive_current_phase(agents, None)
    assert result["current_phase"] == "reviewer"
    phases = {p["name"]: p["state"] for p in result["phases"]}
    assert phases["developer"] == "active"
    assert phases["reviewer"] == "active"


def test_phase_labels_present():
    agents = [AgentState("architect-1", "busy", None, "")]
    result = derive_current_phase(agents, None)
    for p in result["phases"]:
        assert "label" in p
        assert len(p["label"]) > 0
