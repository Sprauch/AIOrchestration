"""Tests for Orchestrator — initialization, config, safety, spawning, cascade, restarts."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from agents.core.config import OrchestratorConfig, AgentConfig, SafetySettings
from agents.core.safety import SafetyChecker, SafetyConfig
from agents.orchestrator import Orchestrator, ROLE_CLASSES


# ── Helpers ───────────────────────────────────────────────

def _minimal_yaml(tmp_path: Path, overrides: dict | None = None) -> str:
    """Write a minimal config.yaml and return its path."""
    data = {
        "system": {"working_dir": ".", "redis_url": "redis://localhost:6379/0"},
        "agents": {
            "pm": {
                "count": 1,
                "cli": "claude",
                "model": "sonnet",
                "subscribes_to": ["system"],
                "publishes_to": ["proposals"],
                "schedule": {"trigger": "startup"},
            },
            "architect": {
                "count": 1,
                "cli": "claude",
                "model": "sonnet",
                "subscribes_to": ["proposals"],
                "publishes_to": ["reviews", "tasks"],
            },
            "developer": {
                "count": 2,
                "cli": "claude",
                "model": "sonnet",
                "subscribes_to": ["tasks"],
                "publishes_to": ["progress", "review-requests"],
                "use_consumer_group": True,
            },
            "reviewer": {
                "count": 1,
                "cli": "claude",
                "model": "sonnet",
                "subscribes_to": ["review-requests"],
                "publishes_to": ["review-results"],
            },
        },
        "safety": {
            "blocked_patterns": ["rm -rf /"],
            "protected_files": [".env"],
            "branch_prefix": "agent/",
        },
    }
    if overrides:
        for section, vals in overrides.items():
            if section in data:
                data[section].update(vals)
            else:
                data[section] = vals
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(data))
    return str(config_file)


class FakeRedis:
    """Minimal fake Redis for bus operations."""

    def __init__(self):
        self._streams: dict[str, list] = {}
        self._hdata: dict[str, dict[str, str]] = {}

    async def xlen(self, key):
        return len(self._streams.get(key, []))

    async def set(self, key, value, ex=None):
        pass

    async def get(self, key):
        return None

    async def hset(self, key, field, value):
        self._hdata.setdefault(key, {})[field] = value

    async def hexists(self, key, field):
        return field in self._hdata.get(key, {})

    async def delete(self, key):
        pass

    async def hincrby(self, key, field, amount):
        self._hdata.setdefault(key, {})
        current = int(self._hdata[key].get(field, 0))
        self._hdata[key][field] = str(current + amount)

    def scan_iter(self, pattern):
        return _EmptyAsyncIter()


class _EmptyAsyncIter:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeBus:
    """In-memory message bus."""

    def __init__(self):
        self.published = []
        self.redis = FakeRedis()

    async def connect(self):
        pass

    async def close(self):
        pass

    async def publish(self, channel, envelope):
        self.published.append((channel, envelope))
        return "fake-id"

    async def subscribe_simple(self, channels, last_ids=None):
        # Block forever (agents consume from this)
        while True:
            await asyncio.sleep(100)
            yield  # pragma: no cover

    async def get_history(self, channel, count=100):
        return []


class FakeCLI:
    """Fake CLI session."""

    def __init__(self, response=""):
        self._response = response

    async def send(self, prompt):
        return self._response

    async def resume(self):
        pass


# ── Initialization & config loading ──────────────────────


def test_orchestrator_default_init():
    """Orchestrator initializes with default attributes."""
    orch = Orchestrator()
    assert orch.config_path == "agents/config.yaml"
    assert orch.config is None
    assert orch.bus is None
    assert orch.agents == {}
    assert orch._running is False
    assert orch._safety is None
    assert orch._shutdown_triggered is False


def test_orchestrator_custom_config_path():
    """Orchestrator accepts custom config_path."""
    orch = Orchestrator(config_path="/tmp/custom.yaml")
    assert orch.config_path == "/tmp/custom.yaml"


def test_load_config_returns_config(tmp_path):
    """load_config() returns an OrchestratorConfig and sets self.config."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    config = orch.load_config()
    assert isinstance(config, OrchestratorConfig)
    assert orch.config is config
    assert "pm" in config.agents
    assert "developer" in config.agents


def test_load_config_picks_up_agent_settings(tmp_path):
    """Config loading parses agent-specific settings like count and use_consumer_group."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    assert orch.config.agents["developer"].count == 2
    assert orch.config.agents["developer"].use_consumer_group is True
    assert orch.config.agents["pm"].count == 1


# ── Safety construction ──────────────────────────────────


def test_build_safety_constructs_checker(tmp_path):
    """_build_safety() returns a SafetyChecker with config values."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    safety = orch._build_safety()
    assert isinstance(safety, SafetyChecker)
    assert safety.config.branch_prefix == "agent/"
    assert ".env" in safety.config.protected_files
    assert "rm -rf /" in safety.config.blocked_patterns


def test_build_safety_custom_prefix(tmp_path):
    """_build_safety() respects a custom branch_prefix from config."""
    config_file = _minimal_yaml(tmp_path, overrides={
        "safety": {"branch_prefix": "bot/", "blocked_patterns": [], "protected_files": []},
    })
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    safety = orch._build_safety()
    assert safety.config.branch_prefix == "bot/"


# ── Agent spawning with role filtering ───────────────────


@pytest.mark.asyncio
async def test_spawn_role_creates_agents(tmp_path):
    """_spawn_role() creates agents for the given role based on count."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_role("developer")

    # developer.count == 2, so two agents should be created
    assert "developer-1" in orch.agents
    assert "developer-2" in orch.agents
    assert len(orch._agent_tasks) == 2

    # Clean up tasks
    for task in orch._agent_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_role_with_count_one(tmp_path):
    """_spawn_role() for a role with count=1 creates a single agent."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_role("pm")

    assert "pm-1" in orch.agents
    assert len(orch.agents) == 1

    for task in orch._agent_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_role_skips_unknown_role(tmp_path):
    """_spawn_role() silently skips a role not in config."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_role("nonexistent")
    assert len(orch.agents) == 0


@pytest.mark.asyncio
async def test_spawn_role_no_duplicates(tmp_path):
    """_spawn_role() does not re-create agents that already exist."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_role("pm")
    first_agent = orch.agents["pm-1"]
    orch._spawn_role("pm")
    assert orch.agents["pm-1"] is first_agent  # same instance

    for task in orch._agent_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_agents_with_filter(tmp_path):
    """_spawn_agents(filter) only spawns the filtered role."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_agents(agent_filter="reviewer")

    assert "reviewer-1" in orch.agents
    # Should not have spawned pm, architect, or developer
    assert "pm-1" not in orch.agents
    assert "architect-1" not in orch.agents
    assert "developer-1" not in orch.agents

    for task in orch._agent_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_agents_without_filter_creates_cascade(tmp_path):
    """_spawn_agents() without filter creates a cascade task."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_agents()

    assert "_cascade" in orch._agent_tasks

    # Clean up
    orch._running = False
    for task in orch._agent_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_role_with_id_override(tmp_path):
    """_spawn_role() uses agent_id_override when provided."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    orch._spawn_role("pm", agent_id_override="pm-custom")

    assert "pm-custom" in orch.agents
    assert "pm-1" not in orch.agents

    for task in orch._agent_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


# ── Cascade spawn ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_spawn_first_stage_immediate(tmp_path):
    """First pipeline stage (PM) spawns immediately (wait_for=None)."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()
    orch._running = True

    # Run cascade briefly, then stop
    task = asyncio.create_task(orch._cascade_spawn())
    await asyncio.sleep(0.2)
    orch._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # PM should be spawned (first stage has wait_for=None)
    assert "pm-1" in orch.agents

    for t in orch._agent_tasks.values():
        t.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_cascade_spawn_resumes_on_existing_messages(tmp_path):
    """Cascade spawn resumes a stage immediately if its stream has existing messages."""
    config_file = _minimal_yaml(tmp_path, overrides={
        "system": {"cascade_poll_interval": 1},
    })
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()

    bus = FakeBus()
    # Pre-populate streams so cascade skips waiting
    bus.redis._streams["stream:proposals"] = ["msg1"]
    bus.redis._streams["stream:tasks"] = ["msg1"]
    bus.redis._streams["stream:review-requests"] = ["msg1"]
    orch.bus = bus
    orch._running = True

    task = asyncio.create_task(orch._cascade_spawn())
    # cascade sleeps 1s between resume stages + poll_interval+1 between waited stages
    # With 3 resume stages (each sleeps 1s) we need ~4s total
    await asyncio.sleep(5.0)
    orch._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # All roles should be spawned
    assert "pm-1" in orch.agents
    assert "architect-1" in orch.agents
    assert "developer-1" in orch.agents
    assert "reviewer-1" in orch.agents

    for t in orch._agent_tasks.values():
        t.cancel()
    await asyncio.gather(*orch._agent_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_cascade_spawn_stops_when_not_running(tmp_path):
    """Cascade exits early when _running becomes False."""
    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()
    orch._safety = orch._build_safety()
    orch.bus = FakeBus()

    # Start not running — should exit immediately
    orch._running = False
    await orch._cascade_spawn()

    assert len(orch.agents) == 0


# ── run_agent restart behavior ────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_normal_exit():
    """Agent that completes start() without error loops until _running is False."""
    orch = Orchestrator()
    orch.config = OrchestratorConfig()
    orch._running = True

    call_count = 0

    async def normal_start():
        nonlocal call_count
        call_count += 1
        # Simulate: agent runs, then orchestrator shuts down
        orch._running = False

    agent = MagicMock()
    agent.start = AsyncMock(side_effect=normal_start)
    agent.cli = MagicMock()

    await orch._run_agent("test-1", agent)

    assert call_count == 1


@pytest.mark.asyncio
async def test_run_agent_crash_and_restart():
    """Agent that crashes is restarted up to max_restarts times."""
    orch = Orchestrator()
    orch.config = OrchestratorConfig(system={"max_restarts": 2, "restart_delay": 0})
    orch._running = True

    call_count = 0

    async def crashing_start():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("boom")
        # Third call succeeds — stop the loop
        orch._running = False

    agent = MagicMock()
    agent.start = AsyncMock(side_effect=crashing_start)
    agent.cli = MagicMock(spec=[])  # no 'resume' attr

    await orch._run_agent("test-1", agent)

    # Should have been called 3 times: 2 crashes + 1 success
    assert call_count == 3


@pytest.mark.asyncio
async def test_run_agent_max_restarts_exceeded():
    """Agent that crashes more than max_restarts times stops being restarted."""
    orch = Orchestrator()
    orch.config = OrchestratorConfig(system={"max_restarts": 1, "restart_delay": 0})
    orch._running = True

    call_count = 0

    async def always_crash():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("always fails")

    agent = MagicMock()
    agent.start = AsyncMock(side_effect=always_crash)
    agent.cli = MagicMock(spec=[])  # no 'resume' attr

    await orch._run_agent("test-1", agent)

    # max_restarts=1 means: initial + 1 restart = 2 total calls
    assert call_count == 2


@pytest.mark.asyncio
async def test_run_agent_cancelled_breaks_loop():
    """CancelledError in agent.start() breaks the restart loop."""
    orch = Orchestrator()
    orch.config = OrchestratorConfig(system={"max_restarts": 5, "restart_delay": 0})
    orch._running = True

    agent = MagicMock()
    agent.start = AsyncMock(side_effect=asyncio.CancelledError())
    agent.cli = MagicMock()

    await orch._run_agent("test-1", agent)

    # Only called once — CancelledError breaks the loop
    agent.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_agent_resumes_cli_on_crash():
    """If agent.cli has a resume() method, it is called after a crash."""
    orch = Orchestrator()
    orch.config = OrchestratorConfig(system={"max_restarts": 1, "restart_delay": 0})
    orch._running = True

    call_count = 0

    async def crash_once():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("crash")
        # Second call succeeds — stop the loop
        orch._running = False

    agent = MagicMock()
    agent.start = AsyncMock(side_effect=crash_once)
    agent.cli = MagicMock()
    agent.cli.resume = AsyncMock()

    await orch._run_agent("test-1", agent)

    agent.cli.resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_agent_stops_when_not_running():
    """If _running becomes False, the restart loop exits."""
    orch = Orchestrator()
    orch.config = OrchestratorConfig(system={"max_restarts": 5, "restart_delay": 0})
    orch._running = True

    call_count = 0

    async def crash_and_disable():
        nonlocal call_count
        call_count += 1
        orch._running = False
        raise RuntimeError("crash")

    agent = MagicMock()
    agent.start = AsyncMock(side_effect=crash_and_disable)
    agent.cli = MagicMock(spec=[])

    await orch._run_agent("test-1", agent)

    # Only one call — _running=False prevents restart
    assert call_count == 1


# ── CLI session creation ─────────────────────────────────


def test_create_cli_session_claude(tmp_path):
    """_create_cli_session creates a ClaudeSession for cli='claude'."""
    from agents.core.cli_session import ClaudeSession

    config_file = _minimal_yaml(tmp_path)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()

    agent_cfg = orch.config.agents["pm"]
    session = orch._create_cli_session("pm", agent_cfg, "pm-1")
    assert isinstance(session, ClaudeSession)
    assert session.json_schema is not None


def test_create_cli_session_codex(tmp_path):
    """_create_cli_session creates a CodexSession for cli='codex'."""
    from agents.core.cli_session import CodexSession

    overrides = {
        "agents": {
            "pm": {
                "count": 1,
                "cli": "codex",
                "model": "o4-mini",
                "subscribes_to": ["system"],
                "publishes_to": ["proposals"],
                "sandbox": "read-only",
                "codex_mode": "exec",
            },
        },
    }
    config_file = _minimal_yaml(tmp_path, overrides=overrides)
    orch = Orchestrator(config_path=config_file)
    orch.load_config()

    agent_cfg = orch.config.agents["pm"]
    session = orch._create_cli_session("pm", agent_cfg, "pm-1")
    assert isinstance(session, CodexSession)
    assert session.json_schema is not None


# ── Pipeline stages constant ─────────────────────────────


def test_pipeline_stages_structure():
    """PIPELINE_STAGES has the expected structure."""
    stages = Orchestrator.PIPELINE_STAGES
    assert len(stages) == 4
    assert stages[0]["wait_for"] is None  # PM starts immediately
    assert stages[1]["wait_for"] == "proposals"
    assert stages[2]["wait_for"] == "tasks"
    assert stages[3]["wait_for"] == "review-requests"


def test_role_classes_contain_all_roles():
    """ROLE_CLASSES maps all four standard roles."""
    assert set(ROLE_CLASSES.keys()) == {"pm", "architect", "developer", "reviewer"}


# ── Shutdown ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_sets_running_false():
    """shutdown() flips _running to False and stops agents."""
    orch = Orchestrator()
    orch._running = True
    orch.bus = FakeBus()
    orch.agents = {}
    orch._agent_tasks = {}

    await orch.shutdown()

    assert orch._running is False


@pytest.mark.asyncio
async def test_shutdown_idempotent():
    """Calling shutdown() when already not running is a no-op."""
    orch = Orchestrator()
    orch._running = False

    # Should not raise
    await orch.shutdown()
    assert orch._running is False
