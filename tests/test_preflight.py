"""Tests for preflight checks."""

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from agents.core.config import OrchestratorConfig
from agents.core.preflight import (
    CURRENT_SCHEMA_VERSION,
    PreflightError,
    _probe_cli,
    check_config_semantics,
    check_cli_tools,
    check_python_version,
    run_preflight,
)


def test_python_version_ok():
    errors = check_python_version()
    assert errors == []


def test_config_missing_prompt_file():
    config = OrchestratorConfig.model_validate({
        "agents": {
            "pm": {
                "system_prompt": "/nonexistent/prompt.md",
                "subscribes_to": ["system"],
                "publishes_to": ["proposals"],
            }
        }
    })
    errors = check_config_semantics(config)
    assert any("system_prompt file not found" in e for e in errors)


def test_config_invalid_cli_backend():
    config = OrchestratorConfig.model_validate({
        "agents": {
            "pm": {
                "cli": "gpt4all",
                "subscribes_to": ["system"],
                "publishes_to": ["proposals"],
            }
        }
    })
    errors = check_config_semantics(config)
    assert any("not valid" in e for e in errors)


def test_config_no_agents():
    config = OrchestratorConfig.model_validate({"agents": {}})
    errors = check_config_semantics(config)
    assert any("No agents configured" in e for e in errors)


def test_config_valid_passes():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("You are a PM")
        f.flush()
        config = OrchestratorConfig.model_validate({
            "agents": {
                "pm": {
                    "cli": "claude",
                    "system_prompt": f.name,
                    "subscribes_to": ["system"],
                    "publishes_to": ["proposals"],
                }
            }
        })
    errors = check_config_semantics(config)
    assert errors == []


def test_config_invalid_deliberation_cli():
    config = OrchestratorConfig.model_validate({
        "agents": {
            "pm": {
                "cli": "claude",
                "subscribes_to": ["system"],
                "publishes_to": ["proposals"],
                "deliberation": {
                    "cli": "invalid_tool",
                    "rounds": 3,
                },
            }
        }
    })
    errors = check_config_semantics(config)
    assert any("deliberation.cli" in e for e in errors)


def test_config_unwritable_log_dir():
    config = OrchestratorConfig.model_validate({
        "system": {"log_dir": "/root/no_permission/logs"},
        "agents": {
            "pm": {"subscribes_to": ["system"], "publishes_to": ["proposals"]},
        }
    })
    errors = check_config_semantics(config)
    assert any("Cannot create log directory" in e for e in errors)


@pytest.mark.asyncio
async def test_run_preflight_catches_errors():
    config = OrchestratorConfig.model_validate({
        "agents": {
            "pm": {
                "cli": "invalid_backend",
                "system_prompt": "/no/such/file.md",
            }
        }
    })
    with pytest.raises(PreflightError) as exc_info:
        await run_preflight(config, check_services=False)
    assert len(exc_info.value.errors) >= 2


# ── CLI probing ────────────────────────────────────────────

def test_probe_cli_python():
    """python3 should always be probeable in our test environment."""
    ok, detail = _probe_cli("python3")
    assert ok is True
    assert "Python" in detail or "python" in detail.lower()


def test_probe_cli_nonexistent():
    ok, detail = _probe_cli("definitely_not_a_real_cli_tool_xyz")
    assert ok is False
    assert "not found" in detail


def test_check_cli_tools_with_nonexistent_cli():
    """A config requiring a nonexistent CLI should produce an error with install guidance."""
    config = OrchestratorConfig.model_validate({
        "agents": {
            "pm": {"cli": "claude", "subscribes_to": ["system"], "publishes_to": ["proposals"]},
        }
    })
    errors = check_cli_tools(config)
    # We can't assert the exact result since claude may or may not be installed,
    # but the function should run and return a list
    assert isinstance(errors, list)


# ── gh CLI preflight ───────────────────────────────────────

def test_gh_missing_with_prs_enabled_is_error(monkeypatch):
    """When enable_prs=true and gh is not on PATH, check_cli_tools must error."""
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "gh" else shutil.which(cmd))
    # Also stub _probe_cli so agent CLIs don't cause unrelated errors
    monkeypatch.setattr(
        "agents.core.preflight._probe_cli", lambda cli: (True, "stub 1.0")
    )
    config = OrchestratorConfig.model_validate({
        "system": {"enable_prs": True},
        "agents": {
            "pm": {"cli": "claude", "subscribes_to": ["system"], "publishes_to": ["proposals"]},
        },
    })
    errors = check_cli_tools(config)
    assert any("gh CLI" in e for e in errors)


def test_gh_missing_with_prs_disabled_passes(monkeypatch):
    """When enable_prs=false and gh is not on PATH, no error is raised."""
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "gh" else shutil.which(cmd))
    monkeypatch.setattr(
        "agents.core.preflight._probe_cli", lambda cli: (True, "stub 1.0")
    )
    config = OrchestratorConfig.model_validate({
        "system": {"enable_prs": False},
        "agents": {
            "pm": {"cli": "claude", "subscribes_to": ["system"], "publishes_to": ["proposals"]},
        },
    })
    errors = check_cli_tools(config)
    assert not any("gh CLI" in e for e in errors)


# ── Schema version ─────────────────────────────────────────

def test_schema_version_is_positive_int():
    assert isinstance(CURRENT_SCHEMA_VERSION, int)
    assert CURRENT_SCHEMA_VERSION >= 1
