# CLAUDE.md

## What is this project?

Agent Orchestrator is a multi-agent collaboration system that autonomously analyzes, improves, and reviews any codebase. It coordinates a team of AI agents (PM, Architect, Developer, Reviewer) as long-running Claude Code or Codex CLI sessions, communicating via Redis streams.

The system is **project-agnostic** — point it at any codebase by setting `AGENT_ORCH_WORKING_DIR` or `system.working_dir` in config.

## Environment

- **Python >= 3.11 required.** The system Python on this machine is 3.9; always use the venv.
- **Always activate the virtual environment before running anything:**
  ```bash
  source agents/.venv/bin/activate
  ```
- If the venv doesn't exist, create it:
  ```bash
  bash agents/setup_venv.sh
  ```
- **Never use the system python3 directly** — it's 3.9 and will fail on 3.11+ syntax.

## Running commands

```bash
source agents/.venv/bin/activate

docker compose up -d                  # Start Redis
pytest tests/ -v                      # Run tests
agent-orchestrator preflight          # Validate environment
agent-orchestrator run                # Start orchestrator service
agent-orchestrator run --debug        # With verbose logging
agent-orchestrator dogfood            # Self-analysis mode (analyze own codebase)
agent-orchestrator web                # Web dashboard
```

## Deployment model

The orchestrator runs as a **host process**, not a container. Claude Code and Codex CLIs require host-level auth (`claude login`, `codex auth`) and native filesystem access to the target codebase. Containerizing adds friction without benefit.

**Redis** can run anywhere — the repo includes a `docker-compose.yaml` for convenience, but any Redis >= 5.0 works (local install, managed service, etc.). Set `AGENT_ORCH_REDIS_URL` to point at your instance.

| Mode | Command | Use case |
|------|---------|----------|
| **Service** | `agent-orchestrator run` | Long-running orchestrator |
| **Batch** | `agent-orchestrator once` | Single cycle, then exit (CI) |
| **Dogfood** | `agent-orchestrator dogfood` | Self-analysis mode (own codebase) |
| **Monitor** | `agent-orchestrator monitor` | TUI observer (separate terminal) |
| **Web** | `agent-orchestrator web` | Web dashboard (browser-based) |
| **Approval** | `agent-orchestrator approve` | Human gate handler (separate terminal) |

**CLI auth is the operator's responsibility** — run `claude login` / `codex auth` before starting. Preflight verifies this by invoking `claude --version` / `codex --version`.

## Config layering

Three layers, highest priority wins:

1. **Defaults** — hardcoded in Pydantic models (`agents/core/config.py`)
2. **YAML** — `agents/config.yaml` for product defaults and role definitions
3. **Env vars** — `AGENT_ORCH_*` prefix for deployment overrides

**Policy:** YAML is for product defaults. Env vars are for deployment/runtime overrides.

## Project structure

```
agents/                    # Main Python package
  core/                    # Infrastructure layer
    message.py             # Envelope + MessageType (wire format)
    message_bus.py         # Redis Streams pub/sub (MAXLEN trimming, blocking XREAD)
    safety.py              # Safety checker (blocked patterns, protected files, branch rules)
    cli_session.py         # ClaudeSession + CodexSession (CLI subprocess wrappers)
    base_agent.py          # AgentProcess base class + DeliberatingAgent + human gate
    config.py              # Pydantic config validation + env var overlay
    preflight.py           # Startup validation (Python, config, Redis, CLI probing, schema version)
  roles/                   # Agent role implementations
    pm_agent.py            # Proposes improvements
    architect_agent.py     # Reviews proposals, writes tech specs
    developer_agent.py     # Implements specs on agent/* branches
    reviewer_agent.py      # Reviews code, approves or requests changes
  prompts/                 # System prompts per role (project-agnostic)
  orchestrator.py          # Pipeline orchestration, agent lifecycle, PR creation
  monitor.py               # Textual TUI dashboard
  approval_console.py      # Human approval gate handler
  run.py                   # CLI entrypoint (subcommands, Python version guard)
  config.yaml              # Product defaults (YAML layer)
tests/                     # Test suite (pytest, 84 tests)
pyproject.toml             # Package definition, CLI entry point, test config
docker-compose.yaml        # Redis (convenience — any Redis >= 5.0 works)
UPGRADING.md               # Version compat, migration, rollback
.github/workflows/
  ci.yml                   # CI: tests on 3.11/3.12/3.13, lint
  release.yml              # Release: PyPI publish, GitHub Release
```

## Architecture decisions

- **Redis Streams** are the message bus. Each message is an `Envelope` (Pydantic model) serialized as JSON.
- **Stream trimming**: every `xadd` includes `MAXLEN ~ 10000` (configurable). Monitor replay capped at 500.
- **Consumer groups** for load-balanced task distribution to developers.
- **Two-level safety:** input-level (prompt check) and output-level (branch, files, text before publish). Escalatable actions emit `HUMAN_GATE` and block via per-gate XREAD until approved.
- **Approval sync**: per-gate response channels (`stream:gate-responses:{gate_id}`) with blocking XREAD — no polling.
- **Orchestrator heartbeat**: writes to Redis every 5s with TTL.
- **PR creation**: retry with audit trail in Redis. Disabled via `AGENT_ORCH_ENABLE_PRS=false`.
- **Config**: Pydantic + pydantic-settings. Three-layer merge (defaults < YAML < env vars).
- **CLI probing**: preflight invokes `claude --version` / `codex --version` to prove CLIs execute. This catches missing/broken installs and hanging auth prompts, but does not verify active session tokens.
- **Metrics**: counters stored in Redis hash `orchestrator:metrics` (messages, errors, gates, PRs). Viewable via `agent-orchestrator status` or `redis-cli HGETALL orchestrator:metrics`.
- **Schema versioning**: `orchestrator:schema_version` in Redis prevents running mismatched versions against existing data.
- **No orchestrator container**: CLIs need host auth and filesystem access. Only Redis is containerized.

## Key conventions

- All configuration lives in `agents/config.yaml` (defaults) + env vars (overrides).
- Agent branches must start with `agent/` (enforced at input and output).
- Prompts in `agents/prompts/` are project-agnostic.
- Tests are in `tests/` at the repo root.

## Testing

```bash
source agents/.venv/bin/activate
pytest tests/ -v
```

Tests do not require Redis or CLI tools. Coverage:
- `test_message.py` — Envelope serialization
- `test_safety.py` — Blocked patterns, protected files, branch rules
- `test_config.py` — Pydantic validation, env var overrides (system, agent, safety)
- `test_routing.py` — Message filtering, channel defaults, response parsing
- `test_preflight.py` — Startup validation, CLI probing, schema version
- `test_output_safety.py` — Output safety, human gate emit/timeout/approval/denial
- `test_integration.py` — Full pipeline cycle with fakes
- `test_cli.py` — Subcommand parsing, backward compat

## Release process

```bash
# Bump version in pyproject.toml, tag, push
git tag v0.1.0
git push origin v0.1.0
```

Triggers: test -> PyPI publish -> GitHub Release. See [UPGRADING.md](UPGRADING.md).

## Common tasks

- **Add a new agent role:** Create `agents/roles/foo_agent.py` extending `AgentProcess`, add prompt, register in `ROLE_CLASSES`, add config.
- **Change config values:** Edit `agents/config.yaml` or set `AGENT_ORCH_*` env vars.
- **Fix a safety rule:** Edit `agents/core/safety.py` + tests.
- **Disable PRs:** Set `AGENT_ORCH_ENABLE_PRS=false`.
