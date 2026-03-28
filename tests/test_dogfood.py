"""Tests for dogfooding support: self-analysis detection, analysis_context, protected-file gating."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.core.config import OrchestratorConfig, SystemConfig
from agents.core.message import Envelope, MessageType
from agents.core.safety import SafetyChecker, SafetyConfig, SafetyViolation


# ── analysis_context config ──


def test_dogfood_mode_default_false(monkeypatch):
    """dogfood_mode defaults to False."""
    monkeypatch.delenv("AGENT_ORCH_DOGFOOD_MODE", raising=False)
    config = SystemConfig()
    assert config.dogfood_mode is False


def test_dogfood_mode_from_env(monkeypatch):
    """dogfood_mode can be set via AGENT_ORCH_DOGFOOD_MODE env var."""
    monkeypatch.setenv("AGENT_ORCH_DOGFOOD_MODE", "true")
    config = SystemConfig()
    assert config.dogfood_mode is True


def test_analysis_context_default_empty(monkeypatch):
    """analysis_context defaults to empty string."""
    monkeypatch.delenv("AGENT_ORCH_ANALYSIS_CONTEXT", raising=False)
    config = SystemConfig()
    assert config.analysis_context == ""


def test_analysis_context_from_env(monkeypatch):
    """analysis_context can be set via AGENT_ORCH_ANALYSIS_CONTEXT env var."""
    monkeypatch.setenv("AGENT_ORCH_ANALYSIS_CONTEXT", "This is infra.")
    config = SystemConfig()
    assert config.analysis_context == "This is infra."


def test_analysis_context_reaches_pm_trigger():
    """PM format_prompt includes analysis_context when present in trigger payload."""
    from agents.roles.pm_agent import PMAgent

    # Create a minimal PMAgent (won't actually run CLI)
    agent = PMAgent.__new__(PMAgent)
    agent.agent_id = "pm-1"
    agent.role = "pm"

    envelope = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
        payload={
            "action": "trigger_analysis",
            "target_role": "pm",
            "analysis_context": "Focus on reliability and observability.",
        },
    )

    prompt = agent.format_prompt(envelope)
    assert "Focus on reliability and observability." in prompt
    assert "Analyze the codebase" in prompt


def test_pm_trigger_without_context():
    """PM format_prompt works normally without analysis_context."""
    from agents.roles.pm_agent import PMAgent

    agent = PMAgent.__new__(PMAgent)
    agent.agent_id = "pm-1"
    agent.role = "pm"

    envelope = Envelope(
        sender_id="orchestrator",
        sender_role="system",
        message_type=MessageType.SYSTEM,
        payload={"action": "trigger_analysis", "target_role": "pm"},
    )

    prompt = agent.format_prompt(envelope)
    assert "Analyze the codebase" in prompt
    assert "analysis_context" not in prompt


# ── Protected-file gating ──


@pytest.mark.asyncio
async def test_protected_file_escalates_to_gate_in_dogfood():
    """In dogfood mode, protected file violation escalates to human gate."""
    from agents.core.base_agent import AgentProcess
    from agents.core.message_bus import MessageBus

    safety = SafetyChecker(SafetyConfig(
        protected_files=["agents/config.yaml"],
    ))

    class DummyAgent(AgentProcess):
        def default_channels(self):
            return {"subscribes_to": ["system"], "publishes_to": ["proposals"]}
        def format_prompt(self, envelope):
            return "test"
        def parse_response(self, raw, source_envelope):
            return []

    bus = MagicMock(spec=MessageBus)
    bus.redis = MagicMock()
    bus.publish = AsyncMock()
    bus.wait_for_message = AsyncMock(return_value=Envelope(
        sender_id="human",
        sender_role="human",
        message_type=MessageType.SYSTEM,
        payload={"action": "approval_granted"},
    ))

    cli = MagicMock()
    agent = DummyAgent(
        agent_id="dev-1",
        role="developer",
        cli_session=cli,
        bus=bus,
        safety=safety,
        dogfood_mode=True,
    )

    envelope = Envelope(
        sender_id="dev-1",
        sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"files_changed": ["agents/config.yaml", "agents/run.py"]},
    )

    result = await agent._check_output_safety(envelope)

    # Should have escalated to human gate (publish called for gate)
    assert bus.publish.called
    gate_call = bus.publish.call_args_list[0]
    assert "human-gates" in gate_call[0][0]
    # And since we returned approval_granted, result should be True
    assert result is True


@pytest.mark.asyncio
async def test_protected_file_denied_blocks_in_dogfood():
    """In dogfood mode, protected file violation denied by human blocks the publish."""
    from agents.core.base_agent import AgentProcess
    from agents.core.message_bus import MessageBus

    safety = SafetyChecker(SafetyConfig(
        protected_files=["agents/config.yaml"],
    ))

    class DummyAgent(AgentProcess):
        def default_channels(self):
            return {"subscribes_to": ["system"], "publishes_to": ["proposals"]}
        def format_prompt(self, envelope):
            return "test"
        def parse_response(self, raw, source_envelope):
            return []

    bus = MagicMock(spec=MessageBus)
    bus.redis = MagicMock()
    bus.publish = AsyncMock()
    bus.wait_for_message = AsyncMock(return_value=Envelope(
        sender_id="human",
        sender_role="human",
        message_type=MessageType.SYSTEM,
        payload={"action": "approval_denied"},
    ))

    cli = MagicMock()
    agent = DummyAgent(
        agent_id="dev-1",
        role="developer",
        cli_session=cli,
        bus=bus,
        safety=safety,
        dogfood_mode=True,
    )

    envelope = Envelope(
        sender_id="dev-1",
        sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"files_changed": ["agents/config.yaml"]},
    )

    result = await agent._check_output_safety(envelope)
    assert result is False


@pytest.mark.asyncio
async def test_protected_file_hard_blocks_in_normal_mode():
    """In normal mode (not dogfood), protected file is hard-blocked, not gated."""
    from agents.core.base_agent import AgentProcess
    from agents.core.message_bus import MessageBus

    safety = SafetyChecker(SafetyConfig(
        protected_files=["agents/config.yaml"],
    ))

    class DummyAgent(AgentProcess):
        def default_channels(self):
            return {"subscribes_to": ["system"], "publishes_to": ["proposals"]}
        def format_prompt(self, envelope):
            return "test"
        def parse_response(self, raw, source_envelope):
            return []

    bus = MagicMock(spec=MessageBus)
    bus.redis = MagicMock()
    bus.publish = AsyncMock()

    cli = MagicMock()
    agent = DummyAgent(
        agent_id="dev-1",
        role="developer",
        cli_session=cli,
        bus=bus,
        safety=safety,
        dogfood_mode=False,  # normal mode — no escalation
    )

    envelope = Envelope(
        sender_id="dev-1",
        sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"files_changed": ["agents/config.yaml"]},
    )

    result = await agent._check_output_safety(envelope)
    # Hard block — no gate published, no approval requested
    assert result is False
    assert not bus.publish.called


@pytest.mark.asyncio
async def test_non_protected_safety_violation_still_hard_blocks():
    """Branch prefix violations are still hard blocks, not escalated."""
    from agents.core.base_agent import AgentProcess
    from agents.core.message_bus import MessageBus

    safety = SafetyChecker(SafetyConfig(
        branch_prefix="agent/",
    ))

    class DummyAgent(AgentProcess):
        def default_channels(self):
            return {"subscribes_to": ["system"], "publishes_to": ["proposals"]}
        def format_prompt(self, envelope):
            return "test"
        def parse_response(self, raw, source_envelope):
            return []

    bus = MagicMock(spec=MessageBus)
    bus.redis = MagicMock()
    bus.publish = AsyncMock()

    cli = MagicMock()
    agent = DummyAgent(
        agent_id="dev-1",
        role="developer",
        cli_session=cli,
        bus=bus,
        safety=safety,
    )

    envelope = Envelope(
        sender_id="dev-1",
        sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"branch_name": "main"},  # violates branch prefix
    )

    result = await agent._check_output_safety(envelope)
    # Hard block — no gate published
    assert result is False
    assert not bus.publish.called
