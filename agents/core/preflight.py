"""Preflight checks — validates environment and config before the orchestrator starts."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agents.core.config import OrchestratorConfig

logger = logging.getLogger(__name__)

REQUIRED_PYTHON = (3, 11)

VALID_CLI_BACKENDS = {"claude", "codex"}

# Redis key storing the schema version written by this software version.
SCHEMA_VERSION_KEY = "orchestrator:schema_version"
CURRENT_SCHEMA_VERSION = 1


class PreflightError(Exception):
    """Raised when a preflight check fails. Contains all errors found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"Preflight checks failed:\n{bullet_list}")


def check_python_version() -> list[str]:
    errors = []
    if sys.version_info < REQUIRED_PYTHON:
        errors.append(
            f"Python >= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} required, "
            f"running {sys.version_info.major}.{sys.version_info.minor}. "
            f"Activate the venv: source agents/.venv/bin/activate"
        )
    return errors


def check_config_semantics(config: OrchestratorConfig) -> list[str]:
    """Validate config semantics beyond structural correctness."""
    errors = []

    if not config.agents:
        errors.append("No agents configured in config.yaml")

    working_dir = Path(config.system.working_dir)
    if not working_dir.is_dir():
        errors.append(
            f"system.working_dir does not exist: {config.system.working_dir}"
        )

    try:
        log_dir = Path(config.system.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        errors.append(f"Cannot create log directory {config.system.log_dir}: {e}")

    for role_name, agent_cfg in config.agents.items():
        if agent_cfg.cli not in VALID_CLI_BACKENDS:
            errors.append(
                f"agents.{role_name}.cli = '{agent_cfg.cli}' is not valid "
                f"(expected: {', '.join(sorted(VALID_CLI_BACKENDS))})"
            )

        if agent_cfg.system_prompt:
            prompt_path = Path(agent_cfg.system_prompt)
            if not prompt_path.exists():
                errors.append(
                    f"agents.{role_name}.system_prompt file not found: {agent_cfg.system_prompt}"
                )

        if agent_cfg.deliberation:
            delib = agent_cfg.deliberation
            if delib.cli not in VALID_CLI_BACKENDS:
                errors.append(
                    f"agents.{role_name}.deliberation.cli = '{delib.cli}' is not valid"
                )
            if delib.system_prompt and not Path(delib.system_prompt).exists():
                errors.append(
                    f"agents.{role_name}.deliberation.system_prompt not found: {delib.system_prompt}"
                )

    return errors


def _probe_cli(cli: str) -> tuple[bool, str]:
    """Invoke a CLI tool to verify it is installed and can execute.

    Runs `<cli> --version` and checks for a successful exit. This proves
    the binary is on PATH and runs without hanging (e.g., on an auth prompt).
    It does NOT prove the CLI is authenticated — a valid session may still
    fail on the first real API call. Full auth validation would require
    making an actual API request, which is out of scope for preflight.

    Returns (ok, detail) where detail is the version string or error message.
    """
    version_flag = "--version"
    try:
        result = subprocess.run(
            [cli, version_flag],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            version = result.stdout.strip().split("\n")[0][:80]
            return True, version
        else:
            stderr = result.stderr.strip()[:200]
            return False, f"exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return False, "not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "timed out (15s) — may indicate an auth prompt"
    except Exception as e:
        return False, str(e)[:200]


def check_cli_tools(config: OrchestratorConfig) -> list[str]:
    """Check that required CLI tools are installed and can execute.

    Invokes `claude --version` / `codex --version` to prove the binary runs.
    This catches: not installed, wrong PATH, broken install, hanging auth prompt.
    It does not catch: expired session tokens (those fail at first agent turn).
    """
    errors = []

    install_hints = {
        "claude": "npm install -g @anthropic-ai/claude-code, then run: claude login",
        "codex": "npm install -g @openai/codex, then run: codex auth",
    }

    needed_clis: set[str] = set()
    for agent_cfg in config.agents.values():
        needed_clis.add(agent_cfg.cli)
        if agent_cfg.deliberation:
            needed_clis.add(agent_cfg.deliberation.cli)

    for cli in needed_clis:
        ok, detail = _probe_cli(cli)
        if ok:
            logger.info("%s CLI ready: %s", cli, detail)
        else:
            hint = install_hints.get(cli, f"install '{cli}'")
            errors.append(f"{cli} CLI not usable ({detail}). Install/auth: {hint}")

    if not shutil.which("gh"):
        if config.system.enable_prs:
            errors.append(
                "gh CLI not found on PATH — required when enable_prs is true. "
                "Install: https://cli.github.com/ or set AGENT_ORCH_ENABLE_PRS=false to disable."
            )
        else:
            logger.info("gh CLI not found (PR creation is disabled)")

    return errors


async def check_redis(config: OrchestratorConfig) -> list[str]:
    errors = []
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(config.system.redis_url, decode_responses=True)
        await r.ping()
        await r.aclose()
    except Exception as e:
        errors.append(
            f"Cannot connect to Redis at {config.system.redis_url}: {e}. "
            f"Start Redis with: docker compose up -d"
        )
    return errors


async def check_schema_version(config: OrchestratorConfig) -> list[str]:
    """Check Redis schema version for compatibility.

    If Redis has data from a newer schema version, refuse to start
    to prevent data corruption. If older or absent, proceed (we're
    forwards-compatible within major versions).
    """
    errors = []
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(config.system.redis_url, decode_responses=True)
        stored = await r.get(SCHEMA_VERSION_KEY)
        if stored is not None:
            stored_version = int(stored)
            if stored_version > CURRENT_SCHEMA_VERSION:
                errors.append(
                    f"Redis contains data from schema version {stored_version}, "
                    f"but this software supports version {CURRENT_SCHEMA_VERSION}. "
                    f"Upgrade agent-orchestrator or flush Redis (redis-cli FLUSHDB)."
                )
        await r.aclose()
    except Exception:
        pass  # Redis connectivity is checked separately
    return errors


async def stamp_schema_version(redis_url: str) -> None:
    """Write the current schema version to Redis after successful startup."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        await r.set(SCHEMA_VERSION_KEY, str(CURRENT_SCHEMA_VERSION))
        await r.aclose()
    except Exception:
        logger.debug("Could not stamp schema version", exc_info=True)


async def run_preflight(config: OrchestratorConfig, check_services: bool = True) -> None:
    """Run all preflight checks. Raises PreflightError if any fail."""
    errors: list[str] = []

    errors.extend(check_python_version())
    errors.extend(check_config_semantics(config))

    if check_services:
        errors.extend(check_cli_tools(config))
        redis_errors = await check_redis(config)
        errors.extend(redis_errors)

        if not redis_errors:
            errors.extend(await check_schema_version(config))

    if errors:
        raise PreflightError(errors)

    logger.info("Preflight checks passed")
