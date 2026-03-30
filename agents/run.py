"""Entrypoint for the multi-agent system.

Commands:
    agent-orchestrator run [--agent ROLE] [--id ID]   Start the orchestrator service
    agent-orchestrator once                           Single improvement cycle, then exit
    agent-orchestrator monitor                        Real-time TUI dashboard
    agent-orchestrator approve                        Interactive human approval console
    agent-orchestrator preflight                      Validate environment and config
    agent-orchestrator status                         One-time status dashboard
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"agent-orchestrator requires Python >= 3.11 "
        f"(running {sys.version_info.major}.{sys.version_info.minor}). "
        f"Activate the venv: source agents/.venv/bin/activate"
    )

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

from agents.core.config import OrchestratorConfig


# ── Logging ────────────────────────────────────────────────

def setup_logging(debug: bool = False, default_level: int = logging.INFO) -> None:
    level = logging.DEBUG if debug else default_level

    # Install a LogRecordFactory that guarantees thread_ctx/agent_ctx on EVERY
    # record — including those from third-party libraries (aiohttp, redis, asyncio)
    # that bypass root logger filters.
    from agents.core.log_context import get_thread_ctx, get_agent_ctx
    _original_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = _original_factory(*args, **kwargs)
        if not hasattr(record, "thread_ctx"):
            record.thread_ctx = get_thread_ctx()
        if not hasattr(record, "agent_ctx"):
            record.agent_ctx = get_agent_ctx()
        return record

    logging.setLogRecordFactory(_factory)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s [%(thread_ctx)s %(agent_ctx)s]: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ── Backward compatibility ─────────────────────────────────

_LEGACY_FLAGS = {
    "--monitor": "monitor",
    "--approve": "approve",
    "--status": "status",
    "--once": "once",
}


def _rewrite_legacy_args(argv: list[str]) -> list[str]:
    """Detect old-style flags and rewrite to subcommands with a deprecation warning."""
    if len(argv) < 2:
        return argv

    for flag, subcommand in _LEGACY_FLAGS.items():
        if flag in argv:
            print(
                f"WARNING: '{flag}' is deprecated. Use 'agent-orchestrator {subcommand}' instead.",
                file=sys.stderr,
            )
            new_argv = [argv[0], subcommand] + [a for a in argv[1:] if a != flag]
            return new_argv

    return argv


# ── Subcommand handlers ────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> None:
    """Start the orchestrator service (long-running)."""
    setup_logging(args.debug)
    config = OrchestratorConfig.from_yaml(args.config)
    working_dir = Path(config.system.working_dir).resolve()
    log = logging.getLogger("agent-orchestrator")
    log.info(
        "Working directory: %s — Use 'agent-orchestrator monitor' in a second terminal to observe progress",
        working_dir,
    )
    from agents.orchestrator import Orchestrator
    orchestrator = Orchestrator(config_path=args.config)
    asyncio.run(orchestrator.start(
        agent_filter=args.agent,
        agent_id_override=args.agent_id,
    ))


def cmd_once(args: argparse.Namespace) -> None:
    """Run a single improvement cycle, then exit."""
    setup_logging(args.debug)
    from agents.orchestrator import Orchestrator
    orchestrator = Orchestrator(config_path=args.config)
    asyncio.run(orchestrator.start_once())


def cmd_monitor(args: argparse.Namespace) -> None:
    """Launch the real-time TUI dashboard."""
    setup_logging(False)
    config = OrchestratorConfig.from_yaml(args.config)
    from agents.monitor import MonitorUI
    monitor = MonitorUI(
        config.system.redis_url,
        stale_agent_threshold=config.system.heartbeat_stale_threshold,
        stuck_thread_threshold=config.system.busy_timeout,
    )
    asyncio.run(monitor.run())


def cmd_web(args: argparse.Namespace) -> None:
    """Launch the web dashboard."""
    setup_logging(False, default_level=logging.WARNING)
    config = OrchestratorConfig.from_yaml(args.config)
    token = args.token or config.system.web_gate_token
    from agents.web import WebDashboard
    dashboard = WebDashboard(
        config.system.redis_url, port=args.port, gate_token=token,
        idle_threshold=config.system.idle_stale_threshold,
        max_change_rounds=config.system.max_change_rounds,
        stream_read_limit=config.system.stream_read_limit,
        max_pending_proposals=config.system.max_pending_proposals,
        max_pending_tasks=config.system.max_pending_tasks,
        max_pending_reviews=config.system.max_pending_reviews,
    )
    asyncio.run(dashboard.start())


def cmd_approve(args: argparse.Namespace) -> None:
    """Launch the interactive human approval console."""
    setup_logging(False)
    redis_url = OrchestratorConfig.from_yaml(args.config).system.redis_url
    from agents.approval_console import HumanApprovalConsole
    console = HumanApprovalConsole(redis_url)
    asyncio.run(console.run())


def cmd_preflight(args: argparse.Namespace) -> None:
    """Validate environment, config, Redis, and CLI tools. Exits 0/1."""
    setup_logging(args.debug)
    from agents.core.preflight import PreflightError, run_preflight
    config = OrchestratorConfig.from_yaml(args.config)
    try:
        asyncio.run(run_preflight(config, check_services=True))
        print("All preflight checks passed.")
    except PreflightError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def cmd_dogfood(args: argparse.Namespace) -> None:
    """Run the orchestrator in self-analysis mode against its own codebase."""
    setup_logging(args.debug)

    config = OrchestratorConfig.from_yaml(args.config)
    import redis
    try:
        r = redis.from_url(config.system.redis_url)
        r.ping()
        r.close()
    except Exception:
        print(
            f"Cannot connect to Redis at {config.system.redis_url}\n"
            f"Start Redis first: docker compose up -d",
            file=sys.stderr,
        )
        sys.exit(1)

    os.environ["AGENT_ORCH_DOGFOOD_MODE"] = "true"
    if not os.environ.get("AGENT_ORCH_ANALYSIS_CONTEXT"):
        os.environ["AGENT_ORCH_ANALYSIS_CONTEXT"] = (
            "This is an infrastructure/developer-tools codebase (the agent orchestrator itself). "
            "Users are developers and operators. Focus on reliability, observability, "
            "developer experience, and correctness. Not end-user features."
        )

    print(
        "\n  Dogfood Mode\n"
        "  ────────────\n"
        "  The orchestrator will analyze and propose changes to its own codebase.\n"
        "  Protected files will require human approval (not silently blocked).\n\n"
        "  Other terminals you may want:\n"
        f"    agent-orchestrator web --config {args.config}       # web dashboard\n"
        f"    agent-orchestrator monitor --config {args.config}   # TUI dashboard\n"
        f"    agent-orchestrator approve --config {args.config}   # approval console\n"
    )

    from agents.orchestrator import Orchestrator
    orchestrator = Orchestrator(config_path=args.config)
    asyncio.run(orchestrator.start())


def cmd_status(args: argparse.Namespace) -> None:
    """Print a one-time dashboard of agent states and stream counts."""
    redis_url = OrchestratorConfig.from_yaml(args.config).system.redis_url
    use_json = getattr(args, "json", False)
    asyncio.run(_run_status(redis_url, use_json))


async def _run_status(redis_url: str, use_json: bool = False) -> None:
    from agents.core.state import load_snapshot

    snapshot = await load_snapshot(redis_url)
    if snapshot is None:
        if use_json:
            print('{"error": "cannot connect to Redis"}')
        else:
            print(f"\033[91mCannot connect to Redis at {redis_url}\033[0m")
        return

    if use_json:
        _render_status_json(snapshot)
    else:
        _render_status_text(snapshot)


def _render_status_json(snapshot) -> None:
    import json as _json
    from dataclasses import asdict
    data = asdict(snapshot)
    print(_json.dumps(data, indent=2, default=str))


def _render_status_text(snapshot) -> None:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"

    print(f"\n{BOLD}{'=' * 55}")
    print(f"  Agent System Status")
    print(f"{'=' * 55}{RESET}\n")

    # Orchestrator heartbeat
    if snapshot.orchestrator_heartbeat:
        age = int(time.time() - snapshot.orchestrator_heartbeat)
        hb_style = GREEN if age < 30 else RED
        print(f"  {BOLD}Orchestrator:{RESET} {hb_style}heartbeat {age}s ago{RESET}\n")
    else:
        print(f"  {BOLD}Orchestrator:{RESET} {DIM}no heartbeat{RESET}\n")

    # Agents
    if snapshot.agents:
        print(f"  {BOLD}Agents:{RESET}")
        for a in snapshot.agents:
            color = {"active": GREEN, "busy": YELLOW, "stopped": RED}.get(a.status, DIM)
            icon = {"active": "+", "busy": "~", "stopped": "-"}.get(a.status, "?")
            hb_str = ""
            if a.heartbeat_age is not None:
                age = int(a.heartbeat_age)
                hb_str = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
            print(f"    {color}{icon}{RESET} {a.agent_id:<16} "
                  f"{color}{a.status:<8}{RESET} "
                  f"{DIM}heartbeat: {hb_str or 'never':<10}{RESET}", end="")
            if a.current_task:
                print(f"  task: {a.current_task[:12]}", end="")
            print()
        print()
    else:
        print(f"  {DIM}No agents registered.{RESET}\n")

    # Streams
    print(f"  {BOLD}Streams:{RESET}")
    for s in snapshot.streams:
        bar = "#" * min(s.count, 30) + ("+" if s.count > 30 else "")
        color = GREEN if s.count > 0 else DIM
        print(f"    {s.name:<20} {color}{s.count:>4}{RESET} {DIM}{bar}{RESET}")
    print()

    # Consumer groups
    has_groups = any(s.groups for s in snapshot.streams)
    if has_groups:
        print(f"  {BOLD}Consumer Groups:{RESET}")
        for s in snapshot.streams:
            for g in s.groups:
                print(f"    {s.name:<20} group:{g.name:<20} "
                      f"consumers: {g.consumers}  pending: {g.pending}")
        print()

    # Metrics
    if snapshot.metrics:
        print(f"  {BOLD}Metrics:{RESET}")
        for key in sorted(snapshot.metrics.keys()):
            print(f"    {key:<30} {GREEN}{snapshot.metrics[key]:>6}{RESET}")
        print()
    else:
        print(f"  {DIM}No metrics recorded yet.{RESET}\n")


# ── Parser ─────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-orchestrator",
        description="Multi-Agent Collaboration System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Configuration (highest priority wins):\n"
            "  1. Defaults   — hardcoded in Pydantic models (agents/core/config.py)\n"
            "  2. YAML       — agents/config.yaml (product defaults, role definitions)\n"
            "  3. Env vars   — AGENT_ORCH_* prefix (deployment/runtime overrides)\n"
            "\n"
            "Examples:\n"
            "  AGENT_ORCH_REDIS_URL=redis://prod:6379 agent-orchestrator run\n"
            "  AGENT_ORCH_CLI_TIMEOUT=1200 agent-orchestrator run   # 20min dev timeout\n"
            "  AGENT_ORCH_DEVELOPER_COUNT=4 agent-orchestrator run  # 4 parallel devs\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- run (service) --
    p_run = subparsers.add_parser("run", help="Start the orchestrator service")
    p_run.add_argument("--agent", choices=["pm", "product_designer", "tech_lead", "developer", "reviewer"],
                       help="Start only a specific agent role")
    p_run.add_argument("--id", dest="agent_id", help="Custom agent ID (e.g., dev-1)")
    p_run.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_run.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_run.set_defaults(func=cmd_run)

    # -- once (batch) --
    p_once = subparsers.add_parser("once", help="Single improvement cycle, then exit")
    p_once.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_once.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_once.set_defaults(func=cmd_once)

    # -- monitor --
    p_mon = subparsers.add_parser("monitor", help="Real-time TUI dashboard")
    p_mon.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_mon.set_defaults(func=cmd_monitor)

    # -- web --
    p_web = subparsers.add_parser("web", help="Web dashboard (browser-based)")
    p_web.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_web.add_argument("--port", type=int, default=8081, help="Port (default: 8081)")
    p_web.add_argument("--token", default=None, help="Bearer token for gate approve/deny endpoints")
    p_web.set_defaults(func=cmd_web)

    # -- approve --
    p_app = subparsers.add_parser("approve", help="Interactive human approval console")
    p_app.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_app.set_defaults(func=cmd_approve)

    # -- preflight --
    p_pre = subparsers.add_parser("preflight", help="Validate environment, config, and dependencies")
    p_pre.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_pre.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_pre.set_defaults(func=cmd_preflight)

    # -- dogfood --
    p_dog = subparsers.add_parser("dogfood", help="Self-analysis mode (analyze own codebase)")
    p_dog.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_dog.add_argument("--debug", action="store_true", help="Enable debug logging")
    p_dog.set_defaults(func=cmd_dogfood)

    # -- status --
    p_st = subparsers.add_parser("status", help="One-time status dashboard")
    p_st.add_argument("--config", default="agents/config.yaml", help="Config file path")
    p_st.add_argument("--json", action="store_true", help="Output as JSON")
    p_st.set_defaults(func=cmd_status)

    return parser


def main() -> None:
    # Backward compatibility: rewrite old-style flags to subcommands
    sys.argv = _rewrite_legacy_args(sys.argv)

    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
