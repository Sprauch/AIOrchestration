"""Tests for configuration validation and env var overrides."""

import tempfile
from pathlib import Path

import pytest
import yaml

from agents.core.config import (
    OrchestratorConfig, AgentConfig, SafetySettings, SystemConfig,
    _apply_agent_env_overrides, _apply_safety_env_overrides,
    check_env_var_typos,
)


# ── Defaults ───────────────────────────────────────────────

def test_default_config():
    config = OrchestratorConfig()
    assert config.system.redis_url == "redis://localhost:6379/0"
    assert config.system.max_concurrent_tasks == 2
    assert config.system.max_restarts == 3
    assert config.system.enable_prs is True
    assert config.safety.branch_prefix == "agent/"


def test_config_from_yaml():
    data = {
        "system": {
            "redis_url": "redis://custom:6380/1",
            "working_dir": ".",
            "max_concurrent_tasks": 4,
        },
        "agents": {
            "pm": {
                "count": 1,
                "cli": "claude",
                "model": "opus",
                "subscribes_to": ["system"],
                "publishes_to": ["proposals"],
            }
        },
        "safety": {
            "blocked_patterns": ["rm -rf"],
            "branch_prefix": "bot/",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        config = OrchestratorConfig.from_yaml(f.name)

    assert config.system.redis_url == "redis://custom:6380/1"
    assert config.system.max_concurrent_tasks == 4
    assert "pm" in config.agents
    assert config.agents["pm"].model == "opus"
    assert config.safety.blocked_patterns == ["rm -rf"]
    assert config.safety.branch_prefix == "bot/"


def test_agent_config_defaults():
    cfg = AgentConfig()
    assert cfg.count == 1
    assert cfg.cli == "claude"
    assert cfg.model == "sonnet"
    assert cfg.permission_mode == "plan"
    assert cfg.use_consumer_group is False


def test_safety_settings_defaults():
    s = SafetySettings()
    assert s.branch_prefix == "agent/"
    assert "main" in s.never_push_to
    assert s.max_files_per_change == 10


def test_system_config_defaults():
    s = SystemConfig()
    assert s.max_restarts == 3
    assert s.idle_timeout == 60
    assert s.max_change_rounds == 3
    assert s.cascade_poll_interval == 2
    assert s.restart_delay == 5
    assert s.stream_maxlen == 50000
    assert s.heartbeat_interval == 5
    assert s.enable_prs is True


# ── System env var overrides ───────────────────────────────

def test_env_var_overrides_redis_url(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_REDIS_URL", "redis://from-env:9999/3")
    s = SystemConfig()
    assert s.redis_url == "redis://from-env:9999/3"


def test_env_var_overrides_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ORCH_REDIS_URL", "redis://env-wins:1234/0")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "system": {"redis_url": "redis://yaml-value:6379/0"},
        "agents": {},
    }))
    config = OrchestratorConfig.from_yaml(str(config_file))
    assert config.system.redis_url == "redis://env-wins:1234/0"


def test_env_var_enable_prs_false(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_ENABLE_PRS", "false")
    s = SystemConfig()
    assert s.enable_prs is False


def test_env_var_max_concurrent_tasks(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_MAX_CONCURRENT_TASKS", "8")
    s = SystemConfig()
    assert s.max_concurrent_tasks == 8


# ── Agent env var overrides ────────────────────────────────

def test_agent_env_override_model(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_PM_MODEL", "haiku")
    agents = {"pm": {"model": "opus", "count": 1}}
    result = _apply_agent_env_overrides(agents)
    assert result["pm"]["model"] == "haiku"


def test_agent_env_override_count(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_DEVELOPER_COUNT", "4")
    agents = {"developer": {"count": 2}}
    result = _apply_agent_env_overrides(agents)
    assert result["developer"]["count"] == 4


def test_agent_env_override_cli(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_REVIEWER_CLI", "codex")
    agents = {"reviewer": {"cli": "claude"}}
    result = _apply_agent_env_overrides(agents)
    assert result["reviewer"]["cli"] == "codex"


def test_agent_env_no_override_when_unset():
    agents = {"pm": {"model": "opus", "count": 1}}
    result = _apply_agent_env_overrides(agents)
    assert result["pm"]["model"] == "opus"
    assert result["pm"]["count"] == 1


def test_agent_env_override_full_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ORCH_PM_MODEL", "haiku")
    monkeypatch.setenv("AGENT_ORCH_DEVELOPER_COUNT", "5")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "system": {"working_dir": "."},
        "agents": {
            "pm": {"model": "opus", "subscribes_to": ["system"], "publishes_to": ["proposals"]},
            "developer": {"count": 2, "subscribes_to": ["tasks"], "publishes_to": ["progress"]},
        },
    }))
    config = OrchestratorConfig.from_yaml(str(config_file))
    assert config.agents["pm"].model == "haiku"
    assert config.agents["developer"].count == 5


# ── Safety env var overrides ───────────────────────────────

def test_safety_env_override_branch_prefix(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_SAFETY_BRANCH_PREFIX", "bot/")
    safety = {"branch_prefix": "agent/"}
    result = _apply_safety_env_overrides(safety)
    assert result["branch_prefix"] == "bot/"


def test_safety_env_override_max_files(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_SAFETY_MAX_FILES_PER_CHANGE", "20")
    safety = {"max_files_per_change": 10}
    result = _apply_safety_env_overrides(safety)
    assert result["max_files_per_change"] == 20


def test_safety_env_no_override_when_unset():
    safety = {"branch_prefix": "agent/", "max_files_per_change": 10}
    result = _apply_safety_env_overrides(safety)
    assert result["branch_prefix"] == "agent/"
    assert result["max_files_per_change"] == 10


# ── Env var typo detection ────────────────────────────────

def test_typo_detection_correct_var_no_warning(monkeypatch):
    """Correct env var (AGENT_ORCH_PM_MODEL) produces no warning."""
    monkeypatch.setenv("AGENT_ORCH_PM_MODEL", "opus")
    warnings = check_env_var_typos()
    assert warnings == []


def test_typo_detection_close_typo_suggests(monkeypatch):
    """AGENT_ORCH_PM_MODLE (typo) suggests AGENT_ORCH_PM_MODEL."""
    monkeypatch.setenv("AGENT_ORCH_PM_MODLE", "opus")
    warnings = check_env_var_typos()
    assert len(warnings) == 1
    assert "AGENT_ORCH_PM_MODLE" in warnings[0]
    assert "AGENT_ORCH_PM_MODEL" in warnings[0]


def test_typo_detection_role_typo_suggests(monkeypatch):
    """AGENT_ORCH_DEVLOPER_MODEL (role typo) suggests AGENT_ORCH_DEVELOPER_MODEL."""
    monkeypatch.setenv("AGENT_ORCH_DEVLOPER_MODEL", "opus")
    warnings = check_env_var_typos()
    assert len(warnings) == 1
    assert "AGENT_ORCH_DEVLOPER_MODEL" in warnings[0]
    assert "AGENT_ORCH_DEVELOPER_MODEL" in warnings[0]


def test_typo_detection_unrecognised_no_suggestion(monkeypatch):
    """Completely unrecognised var returns a warning without a suggestion."""
    monkeypatch.setenv("AGENT_ORCH_XYZZY", "something")
    warnings = check_env_var_typos()
    assert len(warnings) == 1
    assert "AGENT_ORCH_XYZZY" in warnings[0]
    assert "no close match" in warnings[0]


# ── YAML failure paths ────────────────────────────────────

def test_from_yaml_missing_file(tmp_path):
    """from_yaml() raises FileNotFoundError for a non-existent file."""
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        OrchestratorConfig.from_yaml(str(missing))


def test_from_yaml_invalid_syntax(tmp_path):
    """from_yaml() raises yaml.YAMLError for unparseable YAML."""
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("{{invalid: yaml: [unterminated")
    with pytest.raises(yaml.YAMLError):
        OrchestratorConfig.from_yaml(str(bad_file))


def test_from_yaml_invalid_schema(tmp_path):
    """from_yaml() raises ValidationError when types don't match the schema."""
    from pydantic import ValidationError

    schema_file = tmp_path / "bad_schema.yaml"
    schema_file.write_text(yaml.dump({"system": {"max_concurrent_tasks": "not_an_int"}}))
    with pytest.raises(ValidationError):
        OrchestratorConfig.from_yaml(str(schema_file))


def test_typo_detection_logged_from_yaml(monkeypatch, tmp_path, caplog):
    """from_yaml() logs warnings for typos but does not raise."""
    monkeypatch.setenv("AGENT_ORCH_PM_MODLE", "opus")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "system": {"working_dir": "."},
        "agents": {"pm": {"subscribes_to": ["system"], "publishes_to": ["proposals"]}},
    }))
    import logging
    with caplog.at_level(logging.WARNING, logger="agents.core.config"):
        config = OrchestratorConfig.from_yaml(str(config_file))
    # Config loads successfully
    assert "pm" in config.agents
    # Warning was logged
    assert any("AGENT_ORCH_PM_MODLE" in msg for msg in caplog.messages)
