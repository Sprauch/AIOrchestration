"""Pydantic configuration models — validates config.yaml at load time.

Config priority (highest wins): env vars > YAML file > defaults.

Env var surface:
  System:   AGENT_ORCH_REDIS_URL, AGENT_ORCH_WORKING_DIR, AGENT_ORCH_LOG_DIR,
            AGENT_ORCH_MAX_CONCURRENT_TASKS, AGENT_ORCH_ENABLE_PRS, etc.
  Agents:   AGENT_ORCH_{ROLE}_MODEL, AGENT_ORCH_{ROLE}_COUNT,
            AGENT_ORCH_{ROLE}_CLI
  Safety:   AGENT_ORCH_SAFETY_BRANCH_PREFIX, AGENT_ORCH_SAFETY_MAX_FILES_PER_CHANGE

YAML is for product defaults and role definitions.
Env vars are for deployment/runtime overrides.
Deployment-specific values (Redis URL, working dir) should come from env vars, not YAML.
"""

from __future__ import annotations

import difflib
import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agents.core.safety import SafetyConfig

log = logging.getLogger(__name__)


_ENV_PREFIX = "AGENT_ORCH_"


def _env(key: str) -> str | None:
    """Read an env var with the standard prefix. Returns None if unset."""
    return os.environ.get(f"{_ENV_PREFIX}{key}")


class ScheduleConfig(BaseModel):
    trigger: str | None = None


class DeliberationConfig(BaseModel):
    cli: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    rounds: int = 3
    sandbox: str = "read-only"
    codex_mode: str = "exec"
    system_prompt: str | None = None
    permission_mode: str = "plan"
    resume_conversation: bool = False


class AgentConfig(BaseModel):
    count: int = 1
    cli: str = "claude"
    model: str = "sonnet"
    permission_mode: str = "plan"
    system_prompt: str | None = None
    resume_conversation: bool = False
    allowed_tools: str | None = None
    subscribes_to: list[str] = Field(default_factory=list)
    publishes_to: list[str] = Field(default_factory=list)
    use_consumer_group: bool = False
    schedule: ScheduleConfig | None = None
    deliberation: DeliberationConfig | None = None
    # Codex-specific
    codex_mode: str | None = None
    sandbox: str | None = None
    reasoning_effort: str | None = None
    codex_config: dict | None = None


# SafetySettings is an alias for SafetyConfig (from safety.py) to avoid
# duplicating the same fields in two places.
SafetySettings = SafetyConfig


class WorktreeSetupConfig(BaseModel):
    """What a fresh worktree needs before it can build.

    `git worktree add` gives you the tracked files and nothing else, so anything the
    build needs that is gitignored - dependencies, generated data, vendor inputs - has to
    be put there, or an agent produces work it could not have verified.

    Paths are relative to working_dir and are COPIED, not linked: a link means one agent
    writing to generated data corrupts it for every other agent at once, where a copy
    confines the mistake to one worktree.
    """

    # Named copy_paths rather than copy, which would shadow BaseModel.copy.
    copy_paths: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    command_timeout: int = 900
    # Re-read when a worktree is REUSED. If it differs from the main checkout's, the
    # copies are stale and are made again. This is the cost of choosing copy over link,
    # paid deliberately rather than discovered by building against last week's data.
    version_file: str | None = None


class SystemConfig(BaseSettings):
    """System-level config. Env vars with AGENT_ORCH_ prefix override YAML values.

    Priority (highest wins): env vars > YAML file > defaults.
    """

    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX)

    redis_url: str = "redis://localhost:6379/0"
    working_dir: str = "."
    log_dir: str = "agents/logs"
    max_concurrent_tasks: int = 2
    once_timeout: int = 1800
    max_restarts: int = 3
    idle_timeout: int = 60
    max_change_rounds: int = 3
    cascade_poll_interval: int = 2
    restart_delay: int = 5
    stream_maxlen: int = 50000
    heartbeat_interval: int = 5
    enable_prs: bool = True
    gate_timeout: int = 86400
    max_wip: int = 0  # deprecated: if >0, overrides all per-stage limits below
    max_pending_proposals: int = 3  # PM pauses when this many proposals await technical review
    max_pending_tasks: int = 3  # Architect holds approvals when this many tasks await developer
    max_pending_reviews: int = 5  # Slows upstream (PM triggers + tech lead tasks) when reviews back up
    web_gate_token: str | None = None
    # Self-healing
    heartbeat_stale_threshold: int = 60
    busy_timeout: int = 300
    cli_timeout: int = 600
    cli_kill_grace: int = 5  # seconds to wait after SIGTERM before SIGKILL
    pr_max_retries: int = 2
    pr_retry_delay: int = 10
    dedup_max_repeats: int = 1
    dedup_cache_size: int = 10000
    idle_stale_threshold: int = 600  # seconds before an idle agent shows in exceptions
    stream_read_limit: int = 500  # max messages per stream read in web API
    pause_backoff: float = 2.0  # seconds to sleep when agent is paused
    max_payload_bytes: int = 8192
    cli_retry_transient: bool = True
    pipeline_repair_interval: int = 60
    analysis_context: str = ""  # optional context prepended to startup triggers
    dogfood_mode: bool = False  # explicit opt-in for self-analysis behavior
    worktree_setup: WorktreeSetupConfig | None = None  # see WorktreeSetupConfig

    # working_dir existence is checked by preflight, not at config parse time.
    # This allows config to be loaded before the target directory is mounted
    # (e.g., in Docker where volumes appear after config is read).

    @classmethod
    def from_yaml_dict(cls, yaml_data: dict) -> SystemConfig:
        """Build SystemConfig: YAML values as base, env vars override."""
        merged = dict(yaml_data)
        for field_name in cls.model_fields:
            env_key = f"{_ENV_PREFIX}{field_name.upper()}"
            if env_key in os.environ:
                merged[field_name] = os.environ[env_key]
        return cls(**merged)


def _apply_agent_env_overrides(agents: dict[str, dict]) -> dict[str, dict]:
    """Apply AGENT_ORCH_{ROLE}_{FIELD} env vars to agent configs.

    Supported per-role overrides:
      AGENT_ORCH_{ROLE}_MODEL   — e.g. AGENT_ORCH_PM_MODEL=opus
      AGENT_ORCH_{ROLE}_COUNT   — e.g. AGENT_ORCH_DEVELOPER_COUNT=4
      AGENT_ORCH_{ROLE}_CLI     — e.g. AGENT_ORCH_REVIEWER_CLI=codex
    """
    overrideable = {
        "MODEL": ("model", str),
        "COUNT": ("count", int),
        "CLI": ("cli", str),
    }

    for role_name, agent_data in agents.items():
        role_upper = role_name.upper()
        for env_suffix, (field, cast) in overrideable.items():
            env_val = _env(f"{role_upper}_{env_suffix}")
            if env_val is not None:
                agent_data[field] = cast(env_val)

    return agents


def _apply_safety_env_overrides(safety: dict) -> dict:
    """Apply AGENT_ORCH_SAFETY_{FIELD} env vars to safety config.

    Supported overrides:
      AGENT_ORCH_SAFETY_BRANCH_PREFIX
      AGENT_ORCH_SAFETY_MAX_FILES_PER_CHANGE
    """
    val = _env("SAFETY_BRANCH_PREFIX")
    if val is not None:
        safety["branch_prefix"] = val

    val = _env("SAFETY_MAX_FILES_PER_CHANGE")
    if val is not None:
        safety["max_files_per_change"] = int(val)

    return safety


def _known_env_vars(config: OrchestratorConfig | None = None) -> set[str]:
    """Return all recognised AGENT_ORCH_* env var names."""
    names: set[str] = set()

    # System-level fields
    for field_name in SystemConfig.model_fields:
        names.add(f"{_ENV_PREFIX}{field_name.upper()}")

    # Safety overrides
    names.add(f"{_ENV_PREFIX}SAFETY_BRANCH_PREFIX")
    names.add(f"{_ENV_PREFIX}SAFETY_MAX_FILES_PER_CHANGE")

    # Per-role overrides: {PREFIX}{ROLE}_{MODEL|COUNT|CLI}
    role_names: set[str] = {"PM", "PRODUCT_DESIGNER", "TECH_LEAD", "DEVELOPER", "REVIEWER"}
    if config is not None:
        for role in config.agents:
            role_names.add(role.upper())
    for role in role_names:
        for suffix in ("MODEL", "COUNT", "CLI"):
            names.add(f"{_ENV_PREFIX}{role}_{suffix}")

    return names


def check_env_var_typos(config: OrchestratorConfig | None = None) -> list[str]:
    """Check for likely typos in AGENT_ORCH_* env vars.

    Returns a list of human-readable warning strings (one per suspicious var).
    Warnings are advisory — they never raise or block startup.
    """
    known = _known_env_vars(config)
    known_list = sorted(known)
    warnings: list[str] = []

    for var in os.environ:
        if not var.startswith(_ENV_PREFIX):
            continue
        if var in known:
            continue

        # Try to find a close match
        matches = difflib.get_close_matches(var, known_list, n=1, cutoff=0.8)
        if matches:
            warnings.append(
                f"Unrecognised env var {var} — did you mean {matches[0]}?"
            )
        else:
            warnings.append(
                f"Unrecognised env var {var} (no close match found)"
            )

    return warnings


class OrchestratorConfig(BaseModel):
    """Top-level validated configuration.

    Loaded via from_yaml() which applies the three-layer merge:
    1. YAML file (product defaults, role definitions)
    2. Env vars (deployment overrides, secrets)
    3. Pydantic validation (type coercion, constraint checks)
    """

    system: SystemConfig = Field(default_factory=SystemConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    safety: SafetySettings = Field(default_factory=SafetySettings)

    @classmethod
    def from_yaml(cls, path: str) -> OrchestratorConfig:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        # Layer 1: YAML provides base values
        # Layer 2: Env vars override per-section

        # System config — BaseSettings handles its own env vars
        system_yaml = raw.get("system", {})
        system = SystemConfig.from_yaml_dict(system_yaml)
        raw["system"] = system.model_dump()

        # Agent configs — per-role env overrides
        if "agents" in raw:
            raw["agents"] = _apply_agent_env_overrides(raw["agents"])

        # Safety config — selective env overrides
        raw["safety"] = _apply_safety_env_overrides(raw.get("safety", {}))

        # Layer 3: Pydantic validates everything
        config = cls.model_validate(raw)

        # Advisory: warn about possible typos in AGENT_ORCH_* env vars
        for warning in check_env_var_typos(config):
            log.warning(warning)

        return config
