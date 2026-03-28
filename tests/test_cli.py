"""Tests for CLI subcommand parsing and backward compatibility."""

import sys

from agents.run import _build_parser, _rewrite_legacy_args


def test_run_subcommand_parses():
    parser = _build_parser()
    args = parser.parse_args(["run", "--config", "test.yaml", "--debug"])
    assert args.command == "run"
    assert args.config == "test.yaml"
    assert args.debug is True
    assert args.agent is None
    assert args.agent_id is None


def test_run_with_agent_filter():
    parser = _build_parser()
    args = parser.parse_args(["run", "--agent", "pm", "--id", "pm-1"])
    assert args.command == "run"
    assert args.agent == "pm"
    assert args.agent_id == "pm-1"


def test_once_subcommand_parses():
    parser = _build_parser()
    args = parser.parse_args(["once", "--config", "test.yaml"])
    assert args.command == "once"
    assert args.config == "test.yaml"


def test_monitor_subcommand_parses():
    parser = _build_parser()
    args = parser.parse_args(["monitor"])
    assert args.command == "monitor"
    assert args.config == "agents/config.yaml"  # default


def test_approve_subcommand_parses():
    parser = _build_parser()
    args = parser.parse_args(["approve"])
    assert args.command == "approve"


def test_preflight_subcommand_parses():
    parser = _build_parser()
    args = parser.parse_args(["preflight", "--debug"])
    assert args.command == "preflight"
    assert args.debug is True


def test_status_subcommand_parses():
    parser = _build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"


def test_no_subcommand_sets_none():
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.command is None


# ── Backward compatibility ─────────────────────────────────

def test_legacy_monitor_flag_rewritten():
    argv = ["agent-orchestrator", "--monitor", "--config", "test.yaml"]
    result = _rewrite_legacy_args(argv)
    assert result == ["agent-orchestrator", "monitor", "--config", "test.yaml"]


def test_legacy_approve_flag_rewritten():
    argv = ["agent-orchestrator", "--approve"]
    result = _rewrite_legacy_args(argv)
    assert result == ["agent-orchestrator", "approve"]


def test_legacy_once_flag_rewritten():
    argv = ["agent-orchestrator", "--once", "--debug"]
    result = _rewrite_legacy_args(argv)
    assert result == ["agent-orchestrator", "once", "--debug"]


def test_legacy_status_flag_rewritten():
    argv = ["agent-orchestrator", "--status"]
    result = _rewrite_legacy_args(argv)
    assert result == ["agent-orchestrator", "status"]


def test_no_legacy_flags_unchanged():
    argv = ["agent-orchestrator", "run", "--debug"]
    result = _rewrite_legacy_args(argv)
    assert result == argv


def test_empty_argv_unchanged():
    argv = ["agent-orchestrator"]
    result = _rewrite_legacy_args(argv)
    assert result == argv


# ── Startup banner ────────────────────────────────────────

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_cmd_run_emits_working_dir_banner(caplog, tmp_path):
    """cmd_run emits an INFO banner with the resolved working dir and a next-steps hint."""
    from agents.run import cmd_run, _build_parser

    parser = _build_parser()
    args = parser.parse_args(["run", "--config", "agents/config.yaml"])

    mock_orchestrator = MagicMock()
    mock_orchestrator_cls = MagicMock(return_value=mock_orchestrator)

    with (
        patch("agents.run.OrchestratorConfig.from_yaml") as mock_from_yaml,
        patch("agents.orchestrator.Orchestrator", mock_orchestrator_cls),
        patch("asyncio.run"),
    ):
        mock_config = MagicMock()
        mock_config.system.working_dir = str(tmp_path)
        mock_from_yaml.return_value = mock_config

        with caplog.at_level(logging.INFO, logger="agent-orchestrator"):
            cmd_run(args)

    # The banner must contain the resolved absolute path
    assert str(tmp_path.resolve()) in caplog.text
    # The banner must contain a next-steps hint
    assert "agent-orchestrator monitor" in caplog.text


def test_cmd_run_banner_reflects_env_override(caplog, tmp_path, monkeypatch):
    """When AGENT_ORCH_WORKING_DIR is set, the banner reflects the override value."""
    from agents.run import cmd_run, _build_parser

    override_dir = tmp_path / "custom"
    override_dir.mkdir()

    parser = _build_parser()
    args = parser.parse_args(["run", "--config", "agents/config.yaml"])

    mock_orchestrator = MagicMock()
    mock_orchestrator_cls = MagicMock(return_value=mock_orchestrator)

    with (
        patch("agents.run.OrchestratorConfig.from_yaml") as mock_from_yaml,
        patch("agents.orchestrator.Orchestrator", mock_orchestrator_cls),
        patch("asyncio.run"),
    ):
        # Simulate the config returning the env-override value
        mock_config = MagicMock()
        mock_config.system.working_dir = str(override_dir)
        mock_from_yaml.return_value = mock_config

        with caplog.at_level(logging.INFO, logger="agent-orchestrator"):
            cmd_run(args)

    assert str(override_dir.resolve()) in caplog.text
    assert "agent-orchestrator monitor" in caplog.text
