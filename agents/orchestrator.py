"""Orchestrator: spawns agent CLI sessions, bridges Redis, monitors health."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from agents.core.base_agent import AgentProcess
from agents.core.cli_session import ClaudeSession, CodexSession
from agents.core.config import OrchestratorConfig
from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus
from agents.core.metrics import Metrics
from agents.core.output_schema import get_output_schema_for_role
from agents.core.preflight import run_preflight, stamp_schema_version
from agents.core.worktree import remove_worktree
from agents.core.worktree_setup import is_stale, setup_worktree
from agents.core.redis_keys import (
    _active_keys,
    _active_work_count as _active_work_count_fn,
    _claim_keys,
    _clear_active_work_thread,
    _timestamp_to_epoch,
)
from agents.core.safety import SafetyChecker, build_gate_context
from agents.core.thread_guard import THREAD_CYCLES_KEY
from agents.roles.developer_agent import DeveloperAgent
from agents.roles.pm_agent import PMAgent, PMDeliberatingAgent
from agents.roles.product_designer_agent import ProductDesignerAgent
from agents.roles.reviewer_agent import ReviewerAgent
from agents.roles.architect_agent import ArchitectAgent, ArchitectDeliberatingAgent

logger = logging.getLogger(__name__)

ROLE_CLASSES: dict[str, type[AgentProcess]] = {
    "pm": PMAgent,
    "product_designer": ProductDesignerAgent,
    "architect": ArchitectAgent,
    "developer": DeveloperAgent,
    "reviewer": ReviewerAgent,
}

ROLE_DELIBERATING_CLASSES: dict[str, type] = {
    "pm": PMDeliberatingAgent,
    "architect": ArchitectDeliberatingAgent,
}


class Orchestrator:
    """Main process that spawns agents, monitors health, and handles shutdown."""

    def __init__(self, config_path: str = "agents/config.yaml"):
        self.config_path = config_path
        self.config: OrchestratorConfig | None = None
        self.bus: MessageBus | None = None
        self.agents: dict[str, AgentProcess] = {}
        self._agent_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._safety: SafetyChecker | None = None
        self._task_semaphore: asyncio.Semaphore | None = None
        self._shutdown_triggered = False
        self._metrics: Metrics | None = None

    def load_config(self) -> OrchestratorConfig:
        self.config = OrchestratorConfig.from_yaml(self.config_path)
        return self.config

    def _build_safety(self) -> SafetyChecker:
        return SafetyChecker(self.config.safety)

    # Roles that need isolated git working directories to prevent conflicts.
    # Developers write to different branches concurrently.
    # Reviewers run git diff which can be confused by developer checkouts.
    _WORKTREE_ROLES = {"developer", "reviewer"}
    _ACTIVE_WORK_STAGES = ("designs", "proposals", "tasks", "reviews")

    def _get_working_dir(self, role_name: str, agent_id: str, agent_cfg) -> str:
        """Get working directory for an agent.

        Developers and reviewers get isolated git worktrees to prevent
        branch conflicts and dirty-read issues. PM, Product Designer, and Architect share
        the main directory (they only read in plan mode).
        """
        base_dir = self.config.system.working_dir
        if role_name not in self._WORKTREE_ROLES:
            return base_dir
        worktree_dir = str(Path(base_dir).resolve() / ".worktrees" / agent_id)

        # Prune stale worktree references from a previous run
        subprocess.run(["git", "worktree", "prune"], cwd=base_dir, capture_output=True)

        # If directory exists but isn't a valid worktree, clean it up
        if Path(worktree_dir).exists():
            check = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=worktree_dir, capture_output=True,
            )
            if check.returncode == 0:
                # Reuse - but the copied data may have moved on in the main checkout.
                # Building against last week's generated data is the failure a copy
                # trades for isolation, so it is detected rather than discovered.
                setup_cfg = getattr(self.config.system, "worktree_setup", None)
                if setup_cfg and is_stale(base_dir, worktree_dir, setup_cfg.version_file):
                    logger.info("Worktree %s is stale; refreshing its copies", agent_id)
                    setup_worktree(base_dir, worktree_dir, setup_cfg)
                return worktree_dir
            # Stale directory — remove and recreate. force=True: whatever is here is left
            # over from a crashed run and is not worth preserving.
            logger.info("Removing stale worktree dir for %s", agent_id)
            if not remove_worktree(base_dir, worktree_dir, force=True):
                # Adding a worktree over a directory that is still present produces a
                # confusing git error later. Fail here, where the cause is visible.
                raise RuntimeError(
                    f"Could not remove stale worktree {worktree_dir} for {agent_id}. "
                    f"Remove it by hand before restarting."
                )

        try:
            Path(worktree_dir).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "--detach", worktree_dir],
                cwd=base_dir, capture_output=True, check=True,
            )
            logger.info("Created git worktree for %s at %s", agent_id, worktree_dir)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot create worktree for {agent_id}: {exc}. "
                f"Developer/reviewer agents require isolated worktrees."
            ) from exc

        # A checkout alone is not a usable working copy. Anything gitignored that the
        # build needs is absent, and an agent that cannot build cannot verify its own
        # work - so a setup failure fails worktree creation rather than being logged.
        setup_cfg = getattr(self.config.system, "worktree_setup", None)
        if setup_cfg:
            setup_worktree(base_dir, worktree_dir, setup_cfg)

        return worktree_dir

    def _create_cli_session(self, role_name: str, agent_cfg, agent_id: str):
        working_dir = self._get_working_dir(role_name, agent_id, agent_cfg)
        working_dir_resolver = None
        if role_name in self._WORKTREE_ROLES:
            working_dir_resolver = lambda: self._get_working_dir(role_name, agent_id, agent_cfg)
        cli_type = agent_cfg.cli
        cli_timeout = self.config.system.cli_timeout
        system_prompt_file = agent_cfg.system_prompt
        system_prompt = None
        if system_prompt_file and Path(system_prompt_file).exists():
            system_prompt = Path(system_prompt_file).read_text()
        output_schema = get_output_schema_for_role(role_name)

        if cli_type == "codex":
            return CodexSession(
                working_dir=working_dir,
                system_prompt=system_prompt,
                model=agent_cfg.model,
                sandbox=agent_cfg.sandbox or "read-only",
                mode=agent_cfg.codex_mode or "exec",
                reasoning_effort=agent_cfg.reasoning_effort,
                config_overrides=agent_cfg.codex_config or {},
                json_schema=output_schema,
                timeout=cli_timeout,
                kill_grace=self.config.system.cli_kill_grace,
                working_dir_resolver=working_dir_resolver,
            )

        return ClaudeSession(
            working_dir=working_dir,
            system_prompt=system_prompt,
            permission_mode=agent_cfg.permission_mode,
            model=agent_cfg.model,
            allowed_tools=agent_cfg.allowed_tools,
            resume_conversation=agent_cfg.resume_conversation,
            reasoning_effort=agent_cfg.reasoning_effort,
            json_schema=output_schema,
            timeout=cli_timeout,
            kill_grace=self.config.system.cli_kill_grace,
            working_dir_resolver=working_dir_resolver,
        )

    async def _active_work_count(self, stage: str) -> int:
        return await _active_work_count_fn(self.bus.redis, stage)

    async def _has_active_work_state(self) -> bool:
        """Return True if any unresolved-work sets already exist in Redis."""
        try:
            for stage in self._ACTIVE_WORK_STAGES:
                if await self._active_work_count(stage) > 0:
                    return True
            return False
        except Exception:
            return False

    async def _rebuild_active_work_sets(self) -> None:
        """Reconstruct unresolved-work sets from stream history on startup."""
        redis = self.bus.redis
        for stage in self._ACTIVE_WORK_STAGES:
            set_key, msg_key, ts_key = _active_keys(stage)
            claim_key, claim_ts = _claim_keys(stage)
            await redis.delete(set_key)
            await redis.delete(msg_key)
            await redis.delete(ts_key)
            await redis.delete(claim_key)
            await redis.delete(claim_ts)

        ordered_events: list[Envelope] = []
        for stream in ("proposals", "reviews", "tasks", "review-requests", "review-results", "system"):
            try:
                messages = await redis.xrange(f"stream:{stream}")
            except Exception:
                continue
            for _msg_id, data in messages:
                try:
                    ordered_events.append(Envelope.from_json(data["data"]))
                except Exception:
                    continue

        ordered_events.sort(key=lambda env: env.timestamp)
        for env in ordered_events:
            tid = env.thread_id
            if env.message_type == MessageType.PROPOSAL:
                stage = "designs" if env.recipient_role == "product_designer" else "proposals"
                set_key, msg_key, ts_key = _active_keys(stage)
                await redis.sadd(set_key, tid)
                await redis.hset(msg_key, tid, env.to_json())
                await redis.hset(ts_key, tid, str(_timestamp_to_epoch(env.timestamp)))
            elif env.message_type == MessageType.DESIGN_FEEDBACK:
                design_set, design_msg, design_ts = _active_keys("designs")
                design_claim_key, design_claim_ts = _claim_keys("designs")
                proposal_set, proposal_msg, proposal_ts = _active_keys("proposals")
                await redis.srem(design_set, tid)
                await redis.hdel(design_msg, tid)
                await redis.hdel(design_ts, tid)
                await redis.hdel(design_claim_key, tid)
                await redis.hdel(design_claim_ts, tid)
                await redis.sadd(proposal_set, tid)
                await redis.hset(proposal_msg, tid, env.to_json())
                await redis.hset(proposal_ts, tid, str(_timestamp_to_epoch(env.timestamp)))
            elif env.message_type == MessageType.PROPOSAL_REVIEW:
                set_key, msg_key, ts_key = _active_keys("proposals")
                await redis.srem(set_key, tid)
                await redis.hdel(msg_key, tid)
                await redis.hdel(ts_key, tid)
            elif env.message_type == MessageType.TASK_ASSIGNMENT:
                set_key, msg_key, ts_key = _active_keys("tasks")
                await redis.sadd(set_key, tid)
                await redis.hset(msg_key, tid, env.to_json())
                await redis.hset(ts_key, tid, str(_timestamp_to_epoch(env.timestamp)))
            elif env.message_type == MessageType.REVIEW_REQUEST:
                task_set, _task_msg, _task_ts = _active_keys("tasks")
                review_set, review_msg, review_ts = _active_keys("reviews")
                await redis.srem(task_set, tid)
                await redis.sadd(review_set, tid)
                await redis.hset(review_msg, tid, env.to_json())
                await redis.hset(review_ts, tid, str(_timestamp_to_epoch(env.timestamp)))
            elif env.message_type == MessageType.REVIEW_RESULT:
                review_set, review_msg, review_ts = _active_keys("reviews")
                await redis.srem(review_set, tid)
                await redis.hdel(review_msg, tid)
                await redis.hdel(review_ts, tid)
                if env.payload.get("decision") == "changes_requested":
                    task_set, _task_msg, task_ts = _active_keys("tasks")
                    await redis.sadd(task_set, tid)
                    await redis.hset(task_ts, tid, str(_timestamp_to_epoch(env.timestamp)))
                else:
                    _task_set, task_msg, task_ts = _active_keys("tasks")
                    await redis.hdel(task_msg, tid)
                    await redis.hdel(task_ts, tid)
            elif env.message_type == MessageType.SYSTEM:
                action = (env.payload or {}).get("action", "")
                if action in {"thread_abandoned", "pr_created", "pr_merged", "pr_skipped", "pr_closed"}:
                    await _clear_active_work_thread(redis, tid)

        try:
            cycle_counts = await redis.hgetall(THREAD_CYCLES_KEY)
            for field, raw_count in cycle_counts.items():
                if not field.endswith(":cycles"):
                    continue
                try:
                    count = int(raw_count)
                except Exception:
                    continue
                if count >= self.config.system.max_change_rounds:
                    await _clear_active_work_thread(redis, field[:-7])
        except Exception:
            pass

    def _create_agent(self, role_name: str, agent_id: str) -> AgentProcess:
        agent_cfg = self.config.agents[role_name]
        cli_session = self._create_cli_session(role_name, agent_cfg, agent_id)

        config_channels = {}
        if agent_cfg.subscribes_to:
            config_channels["subscribes_to"] = agent_cfg.subscribes_to
        if agent_cfg.publishes_to:
            config_channels["publishes_to"] = agent_cfg.publishes_to

        common_kwargs = dict(
            agent_id=agent_id,
            role=role_name,
            cli_session=cli_session,
            bus=self.bus,
            safety=self._safety,
            config_channels=config_channels or None,
            log_dir=self.config.system.log_dir,
            task_semaphore=self._task_semaphore,
            use_consumer_group=agent_cfg.use_consumer_group,
            gate_timeout=self.config.system.gate_timeout,
            max_change_rounds=self.config.system.max_change_rounds,
            dedup_max_repeats=self.config.system.dedup_max_repeats,
            dedup_cache_size=self.config.system.dedup_cache_size,
            max_pending_proposals=self.config.system.max_pending_proposals,
            max_pending_tasks=self.config.system.max_pending_tasks,
            max_pending_reviews=self.config.system.max_pending_reviews,
            pause_backoff=self.config.system.pause_backoff,
            dogfood_mode=getattr(self.config.system, "dogfood_mode", False),
            claim_min_idle_time=(self.config.system.cli_timeout + 30) * 1000,
        )

        delib_cfg = agent_cfg.deliberation
        if delib_cfg and role_name in ROLE_DELIBERATING_CLASSES:
            secondary_session = self._create_secondary_cli(delib_cfg, agent_id)
            agent_class = ROLE_DELIBERATING_CLASSES[role_name]
            return agent_class(
                secondary_cli=secondary_session,
                deliberation_rounds=delib_cfg.rounds,
                **common_kwargs,
            )

        agent_class = ROLE_CLASSES[role_name]
        return agent_class(**common_kwargs)

    def _create_secondary_cli(self, delib_cfg, agent_id: str):
        working_dir = self.config.system.working_dir
        cli_type = delib_cfg.cli
        cli_timeout = self.config.system.cli_timeout
        model = delib_cfg.model
        system_prompt_file = delib_cfg.system_prompt
        system_prompt = None
        if system_prompt_file:
            p = Path(system_prompt_file)
            if p.exists():
                system_prompt = p.read_text()

        if cli_type == "codex":
            return CodexSession(
                working_dir=working_dir,
                system_prompt=system_prompt,
                model=model,
                sandbox=delib_cfg.sandbox,
                mode=delib_cfg.codex_mode,
                reasoning_effort=delib_cfg.reasoning_effort,
                timeout=cli_timeout,
            )

        return ClaudeSession(
            working_dir=working_dir,
            system_prompt=system_prompt,
            permission_mode=delib_cfg.permission_mode,
            model=model or "sonnet",
            resume_conversation=delib_cfg.resume_conversation,
            reasoning_effort=delib_cfg.reasoning_effort,
            timeout=cli_timeout,
        )

    PIPELINE_STAGES = [
        {"roles": ["pm"], "wait_for": None},
        {"roles": ["product_designer"], "wait_for": "proposals"},
        {"roles": ["architect"], "wait_for": ["proposals", "design-feedback"]},
        {"roles": ["developer"], "wait_for": "tasks"},
        {"roles": ["reviewer"], "wait_for": "review-requests"},
    ]

    def _spawn_agents(self, agent_filter: str | None = None, agent_id_override: str | None = None) -> None:
        if agent_filter:
            self._spawn_role(agent_filter, agent_id_override)
        else:
            cascade_task = asyncio.create_task(self._cascade_spawn(agent_id_override))
            self._agent_tasks["_cascade"] = cascade_task

    def _spawn_role(self, role_name: str, agent_id_override: str | None = None) -> None:
        if role_name not in self.config.agents:
            return

        agent_cfg = self.config.agents[role_name]
        count = agent_cfg.count
        for i in range(count):
            aid = agent_id_override or (f"{role_name}-{i + 1}" if count > 1 else f"{role_name}-1")

            if aid in self.agents:
                continue

            try:
                agent = self._create_agent(role_name, aid)
            except Exception:
                logger.exception("Failed to create agent %s — skipping", aid)
                continue
            self.agents[aid] = agent
            task = asyncio.create_task(self._run_agent(aid, agent))
            self._agent_tasks[aid] = task
            logger.info("Spawned agent %s (role=%s)", aid, role_name)

    async def _cascade_spawn(self, agent_id_override: str | None = None) -> None:
        poll_interval = self.config.system.cascade_poll_interval

        for idx, stage in enumerate(self.PIPELINE_STAGES):
            if not self._running:
                break

            wait_stream = stage["wait_for"]

            if wait_stream is None:
                for role in stage["roles"]:
                    self._spawn_role(role, agent_id_override)
                logger.info("Pipeline stage %d started: %s", idx + 1, stage["roles"])
                continue

            wait_streams = wait_stream if isinstance(wait_stream, list) else [wait_stream]
            try:
                existing = 0
                for stream_name in wait_streams:
                    existing = max(existing, await self.bus.redis.xlen(f"stream:{stream_name}"))
            except Exception:
                existing = 0

            if existing > 0:
                for role in stage["roles"]:
                    self._spawn_role(role, agent_id_override)
                logger.info(
                    "Pipeline stage %d resumed: %s (streams:%s have %d messages)",
                    idx + 1, stage["roles"], ",".join(wait_streams), existing,
                )
                await asyncio.sleep(1)
                continue

            logger.info(
                "Pipeline stage %d (%s) waiting for streams:%s...",
                idx + 1, stage["roles"], ",".join(wait_streams),
            )

            while self._running:
                try:
                    ready = False
                    for stream_name in wait_streams:
                        stream_len = await self.bus.redis.xlen(f"stream:{stream_name}")
                        if stream_len > 0:
                            ready = True
                            break
                    if ready:
                        break
                except Exception:
                    pass
                await asyncio.sleep(poll_interval)

            if not self._running:
                break

            for role in stage["roles"]:
                self._spawn_role(role, agent_id_override)
            logger.info("Pipeline stage %d started: %s", idx + 1, stage["roles"])
            await asyncio.sleep(poll_interval + 1)

    async def start(self, agent_filter: str | None = None, agent_id_override: str | None = None) -> None:
        self.load_config()
        await run_preflight(self.config)
        self._safety = self._build_safety()

        max_tasks = self.config.system.max_concurrent_tasks
        if max_tasks > 0:
            self._task_semaphore = asyncio.Semaphore(max_tasks)
            logger.info("Concurrency limit: %d simultaneous tasks", max_tasks)

        redis_url = self.config.system.redis_url
        self.bus = MessageBus(redis_url, stream_maxlen=self.config.system.stream_maxlen)
        await self.bus.connect()
        await stamp_schema_version(redis_url)
        self._metrics = Metrics(self.bus.redis)

        self._running = True
        self._install_signal_handlers()

        # Clean up stale state from previous runs
        self._cleanup_stale_worktrees()
        await self._cleanup_stale_agent_keys()
        if await self._has_active_work_state():
            logger.info("Using existing active-work state from Redis")
        else:
            logger.info("Rebuilding active-work state from stream history")
            await self._rebuild_active_work_sets()
        await self._cleanup_completed_branches()

        try:
            existing_proposals = await self._active_work_count("designs") + await self._active_work_count("proposals")
        except Exception:
            existing_proposals = 0

        if existing_proposals > 0:
            logger.info("Resuming pipeline (%d unresolved proposals/design items)", existing_proposals)
        else:
            logger.info("Starting fresh pipeline")

        self._spawn_agents(agent_filter, agent_id_override)

        await asyncio.sleep(2)
        await self._publish_startup_triggers()
        await self._repair_stalled_pipeline()

        if self.config.system.enable_prs:
            pr_task = asyncio.create_task(self._pr_creation_loop())
            self._agent_tasks["_pr_creator"] = pr_task
            pr_watch_task = asyncio.create_task(self._pr_status_loop())
            self._agent_tasks["_pr_watcher"] = pr_watch_task
        else:
            logger.info("PR creation disabled (enable_prs=false)")

        hb_task = asyncio.create_task(self._heartbeat_loop())
        self._agent_tasks["_heartbeat"] = hb_task

        try:
            while self._running:
                pending = [t for t in self._agent_tasks.values() if not t.done()]
                if not pending:
                    break
                await asyncio.wait(pending, timeout=5)
        except asyncio.CancelledError:
            pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()

        def _handle_signal() -> None:
            if not self._shutdown_triggered:
                self._shutdown_triggered = True
                asyncio.create_task(self.shutdown())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

    _REPAIR_LOCK_KEY = "orchestrator:repair_lock"

    async def _repair_stalled_pipeline(self) -> None:
        # Claim a short-lived lock to prevent concurrent repairs
        acquired = await self.bus.redis.set(
            self._REPAIR_LOCK_KEY, "1", nx=True, ex=self.config.system.pipeline_repair_interval,
        )
        if not acquired:
            return

        steps = [
            ("designs", "proposals", "design-feedback", "proposals consumed but no design feedback"),
            ("proposals", "proposals", "reviews", "proposals consumed but no reviews"),
            ("tasks", "tasks", "review-requests", "tasks consumed but no review requests"),
            ("reviews", "review-requests", "review-results", "review requests consumed but no results"),
        ]

        stale_after = max(self.config.system.pipeline_repair_interval * 2, 30)

        for stage, input_stream, output_stream, description in steps:
            try:
                active_key, msg_key, ts_key = _active_keys(stage)
                thread_ids = list(await self.bus.redis.smembers(active_key))
            except Exception:
                continue

            if not thread_ids:
                continue

            for thread_id in thread_ids:
                try:
                    raw_ts = await self.bus.redis.hget(ts_key, thread_id)
                    if not raw_ts:
                        continue
                    entered_at = float(raw_ts)
                    age = time.time() - entered_at
                    if age < stale_after:
                        continue

                    raw_env = await self.bus.redis.hget(msg_key, thread_id)
                    if not raw_env:
                        continue

                    # Check if the output stream already has newer activity for
                    # this thread.  If so, the pipeline is progressing — the
                    # active-set just hasn't been updated yet.  Skip re-queue to
                    # avoid duplicate proposals / tasks.
                    has_progress = False
                    try:
                        recent = await self.bus.redis.xrevrange(
                            f"stream:{output_stream}", count=50,
                        )
                        for _mid, data in recent:
                            try:
                                env = Envelope.from_json(data["data"])
                                if env.thread_id == thread_id:
                                    has_progress = True
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass
                    if has_progress:
                        logger.debug(
                            "Stall check: %s thread %s has output-stream activity, skipping re-queue",
                            stage, thread_id[:8],
                        )
                        continue

                    stall_key = f"orchestrator:stall_surfaced:{stage}:{thread_id}"
                    first_time = await self.bus.redis.set(stall_key, "1", nx=True, ex=600)
                    if not first_time:
                        continue

                    await self.bus.publish("system", Envelope(
                        sender_id="orchestrator",
                        sender_role="system",
                        message_type=MessageType.SYSTEM,
                        payload={
                            "action": "pipeline_stall",
                            "input_stream": input_stream,
                            "output_stream": output_stream,
                            "description": description,
                            "thread_id": thread_id,
                            "active_stage": stage,
                            "age_seconds": int(age),
                        },
                    ))

                    await self.bus.redis.xadd(f"stream:{input_stream}", {"data": raw_env})
                    # Reset the timestamp so we don't immediately re-trigger
                    await self.bus.redis.hset(ts_key, thread_id, str(time.time()))
                    logger.warning(
                        "Pipeline stall: %s (thread: %s, age=%ss) — re-queued once",
                        description, thread_id[:8], int(age),
                    )
                    if self._metrics:
                        await self._metrics.increment("pipeline:stalls")
                except Exception:
                    logger.exception("Failed to surface stalled pipeline for %s/%s", stage, thread_id[:8])

    async def _publish_startup_triggers(self) -> None:
        # Per-stage check: count unresolved design + proposal backlog for PM trigger decision
        proposals = await self._active_work_count("designs") + await self._active_work_count("proposals")
        max_proposals = self.config.system.max_pending_proposals
        if proposals >= max_proposals:
            logger.info(
                "Skipping PM trigger: %d unresolved proposals/design items (max %d)",
                proposals, max_proposals,
            )
            return

        # Review backpressure: don't start fresh work if reviews are backed up
        reviews = await self._active_work_count("reviews")
        max_reviews = self.config.system.max_pending_reviews
        if reviews >= max_reviews:
            logger.info(
                "Skipping PM trigger: %d unresolved reviews (max %d)",
                reviews, max_reviews,
            )
            return

        # Read product focus: dogfood uses agents/dogfood_focus.md,
        # normal mode reads PRODUCT_FOCUS.md from the target repo
        focus = ""
        try:
            if getattr(self.config.system, "dogfood_mode", False):
                focus_path = Path(__file__).parent / "dogfood_focus.md"
            else:
                focus_path = Path(self.config.system.working_dir) / "PRODUCT_FOCUS.md"
            if focus_path.exists():
                focus = focus_path.read_text()[:2000]
                logger.info("Loaded product focus from %s (%d chars)", focus_path, len(focus))
        except Exception:
            pass

        for role_name, agent_cfg in self.config.agents.items():
            if agent_cfg.schedule and agent_cfg.schedule.trigger == "startup":
                payload = {
                    "action": "trigger_analysis",
                    "target_role": role_name,
                    "message": f"Startup trigger for {role_name}",
                }
                if focus:
                    payload["product_focus"] = focus
                await self.bus.publish("system", Envelope(
                    sender_id="orchestrator",
                    sender_role="system",
                    message_type=MessageType.SYSTEM,
                    payload=payload,
                ))
                logger.info("Published startup trigger for %s", role_name)

    PR_SET_KEY = "orchestrator:created_prs"
    @property
    def PR_MAX_RETRIES(self):
        return self.config.system.pr_max_retries

    @property
    def PR_RETRY_DELAY(self):
        return self.config.system.pr_retry_delay

    async def _pr_creation_loop(self) -> None:
        async for envelope in self.bus.subscribe_simple(["review-results"]):
            if not self._running:
                break
            if envelope.message_type != MessageType.REVIEW_RESULT:
                continue
            if envelope.payload.get("decision") != "approved":
                continue

            # Atomic claim: HSETNX returns True only for the first caller.
            # This prevents concurrent PR creation from replayed reviews or
            # multiple orchestrator instances.
            claimed = await self.bus.redis.hsetnx(
                self.PR_SET_KEY, envelope.thread_id, f"claimed|{time.time():.0f}|pending",
            )
            if not claimed:
                continue

            branch = await self._find_branch_for_thread(envelope.thread_id)
            if not branch:
                logger.warning("Approved review but no branch found for thread %s", envelope.thread_id[:8])
                continue

            summary = envelope.payload.get("summary", "Agent-generated improvement")
            title = summary[:70] if summary else f"Agent improvement ({branch})"

            await self._create_pr_with_retry(envelope.thread_id, branch, title, summary)

    async def _create_pr_with_retry(
        self, thread_id: str, branch: str, title: str, summary: str,
    ) -> None:
        """Create a PR via `gh` with retries and audit trail in Redis."""
        if not shutil.which("gh"):
            logger.warning("gh CLI not found — skipping PR creation for %s", branch)
            await self._audit_pr(thread_id, "skipped", "gh not found")
            return

        # Human approval gate for PR creation (if configured)
        if "create_pr" in self.config.safety.human_approval_required:
            gate = Envelope(
                sender_id="orchestrator",
                sender_role="system",
                message_type=MessageType.HUMAN_GATE,
                payload={
                    "action": "create_pr",
                    "reason": f"PR for: {title}",
                    "context": build_gate_context(
                        operation="create_pr",
                        branch=branch,
                        safety_summary="passed",
                        escalation_reason=f"PR creation requires approval",
                        extra={"pr_title": title, "thread": thread_id[:8]},
                    ),
                },
                thread_id=thread_id,
            )
            await self.bus.publish("human-gates", gate)
            logger.info("Awaiting human approval for PR creation: %s (gate %s)", branch, gate.id[:8])
            await self._audit_pr(thread_id, "awaiting_approval", f"branch={branch}")

            response = await self.bus.wait_for_message(
                f"gate-responses:{gate.id}",
                timeout_ms=self.config.system.gate_timeout * 1000,
            )
            if response is None or response.payload.get("action") != "approval_granted":
                reason = "timed out" if response is None else "denied"
                logger.info("PR creation %s for %s", reason, branch)
                await self._audit_pr(thread_id, "skipped", f"human {reason}")
                return

        branch_ready, detail = await self._prepare_branch_for_pr(branch)
        if not branch_ready:
            logger.warning("Skipping PR creation for %s: %s", branch, detail)
            await self._audit_pr(thread_id, "skipped", detail)
            return

        await self._audit_pr(thread_id, "pending", f"branch={branch}")

        last_error = ""
        for attempt in range(1, self.PR_MAX_RETRIES + 2):  # +2 for 0-indexed + initial attempt
            logger.info("Creating PR for branch %s (attempt %d)", branch, attempt)
            try:
                proc = await self._run_subprocess(
                    "gh", "pr", "create",
                    "--base", "main",
                    "--head", branch,
                    "--title", title,
                    "--body", f"## Summary\n{summary}\n\n---\nCreated by agent system.",
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode == 0:
                    pr_url = stdout.decode().strip()
                    logger.info("PR created: %s", pr_url)
                    # Shield audit from cancellation — PR is already created on GitHub,
                    # losing the audit entry makes it invisible to the dashboard.
                    await asyncio.shield(self._audit_pr(thread_id, pr_url, f"attempt={attempt}"))
                    if self._metrics:
                        await self._metrics.increment("prs:created")
                    return

                last_error = stderr.decode()[:300]

                # Don't retry on client errors (already exists, auth, etc.)
                if "already exists" in last_error.lower():
                    logger.info("PR already exists for branch %s", branch)
                    await self._audit_pr(thread_id, "exists", last_error)
                    return

                logger.warning(
                    "gh pr create attempt %d failed: %s", attempt, last_error,
                )

            except Exception as e:
                last_error = str(e)[:300]
                logger.warning("gh pr create attempt %d error: %s", attempt, last_error)

            if attempt <= self.PR_MAX_RETRIES:
                await asyncio.sleep(self.PR_RETRY_DELAY)

        # All retries exhausted
        logger.error("PR creation failed after %d attempts for %s", self.PR_MAX_RETRIES + 1, branch)
        await self._audit_pr(thread_id, f"failed", last_error)
        if self._metrics:
            await self._metrics.increment("prs:failed")

    async def _prepare_branch_for_pr(self, branch: str, base: str = "main") -> tuple[bool, str]:
        """Verify a branch is PR-ready and push it if GitHub cannot see it yet."""
        local_exists, _ = await self._git_ref_exists("refs/heads", branch)
        if not local_exists:
            return False, f"local branch missing: {branch}"

        has_diff, diff_detail = await self._git_branch_has_diff(branch, base)
        if not has_diff:
            return False, diff_detail

        remote_exists, remote_detail = await self._git_ref_exists("refs/remotes/origin", branch)
        if remote_exists:
            return True, f"remote branch exists: {branch}"

        pushed, push_detail = await self._git_push_branch(branch)
        if not pushed:
            return False, push_detail

        remote_exists, remote_detail = await self._git_ref_exists("refs/remotes/origin", branch)
        if remote_exists:
            return True, push_detail
        return False, remote_detail

    async def _git_ref_exists(self, ref_prefix: str, branch: str) -> tuple[bool, str]:
        proc = await self._run_subprocess("git", "show-ref", "--verify", f"{ref_prefix}/{branch}")
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, stdout.decode().strip() or f"{ref_prefix}/{branch}"
        detail = stderr.decode().strip() or stdout.decode().strip() or f"{ref_prefix}/{branch} missing"
        return False, detail[:300]

    async def _git_branch_has_diff(self, branch: str, base: str) -> tuple[bool, str]:
        proc = await self._run_subprocess("git", "rev-list", "--count", f"{base}..{branch}")
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode().strip() or stdout.decode().strip() or "git rev-list failed"
            return False, detail[:300]

        count_text = stdout.decode().strip() or "0"
        try:
            count = int(count_text)
        except ValueError:
            return False, f"invalid diff count for {branch}: {count_text[:80]}"

        if count <= 0:
            return False, f"no diff vs {base}: {branch}"
        return True, f"{count} commits ahead of {base}"

    async def _git_push_branch(self, branch: str) -> tuple[bool, str]:
        proc = await self._run_subprocess("git", "push", "-u", "origin", branch)
        stdout, stderr = await proc.communicate()
        detail = stderr.decode().strip() or stdout.decode().strip() or f"pushed {branch}"
        if proc.returncode == 0:
            return True, detail[:300]
        return False, f"push failed: {detail[:280]}"

    async def _run_subprocess(self, *args: str):
        return await asyncio.create_subprocess_exec(
            *args,
            cwd=self.config.system.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _audit_pr(self, thread_id: str, status: str, detail: str = "") -> None:
        """Write PR creation audit entry to Redis hash and publish a system event."""

        value = f"{status}|{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}|{detail}"
        await self.bus.redis.hset(self.PR_SET_KEY, thread_id, value)

        # Publish system event so SSE/dashboard picks it up immediately
        if "merged" in detail:
            pr_action = "pr_merged"
        elif "closed" in detail:
            pr_action = "pr_closed"
        elif status.startswith("http"):
            pr_action = "pr_created"
        else:
            pr_action = f"pr_{status}"
        if pr_action in {"pr_created", "pr_merged", "pr_skipped", "pr_closed"}:
            await _clear_active_work_thread(self.bus.redis, thread_id)
        # Clean up branch on terminal PR outcomes
        if pr_action in {"pr_merged", "pr_skipped", "pr_closed"}:
            await self._cleanup_thread_branch(thread_id)
        await self.bus.publish("system", Envelope(
            sender_id="orchestrator",
            sender_role="system",
            message_type=MessageType.SYSTEM,
            payload={
                "action": pr_action,
                "thread_id": thread_id,
                "pr_status": status,
                "detail": detail[:200],
            },
            thread_id=thread_id,
        ))

    async def _pr_status_loop(self) -> None:
        """Periodically check open PRs for merge/close status via `gh pr view`."""
        while self._running:
            await asyncio.sleep(60)  # check every 60s
            if not shutil.which("gh"):
                continue
            try:
                raw = await self.bus.redis.hgetall(self.PR_SET_KEY)
            except Exception:
                continue

            for thread_id, value in raw.items():
                parts = value.split("|", 2)
                status = parts[0]
                # Only check PRs that have a URL (successfully created)
                if not status.startswith("http"):
                    continue
                # Skip if already tracked as merged/closed
                if "merged" in value or "closed" in value:
                    continue

                try:
                    proc = await asyncio.create_subprocess_exec(
                        "gh", "pr", "view", status, "--json", "state",
                        cwd=self.config.system.working_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                    if proc.returncode != 0:
                        continue
                    data = json.loads(stdout.decode())
                    state = data.get("state", "").upper()

                    if state == "MERGED":
                        logger.info("PR merged: %s (thread %s)", status, thread_id[:8])
                        await self._audit_pr(thread_id, status, "merged")
                    elif state == "CLOSED":
                        logger.info("PR closed without merge: %s (thread %s)", status, thread_id[:8])
                        await self._audit_pr(thread_id, status, "closed")
                except Exception:
                    continue

    async def _find_branch_for_thread(self, thread_id: str) -> str | None:
        try:
            # Check Redis hash first (set by tech lead on task assignment)
            branch = await self.bus.redis.hget("orchestrator:thread_branches", thread_id)
            if branch:
                return branch
            # Fall back to stream history scan
            for stream in ("tasks", "review-requests"):
                history = await self.bus.get_history(stream, count=1000)
                for env in history:
                    if env.thread_id == thread_id:
                        return env.payload.get("branch_name")
        except Exception:
            logger.exception("Error searching for branch in thread %s", thread_id[:8])
        return None

    async def _run_agent(self, agent_id: str, agent: AgentProcess) -> None:
        max_restarts = self.config.system.max_restarts
        restart_delay = self.config.system.restart_delay
        restarts = 0

        while self._running and restarts <= max_restarts:
            try:
                await agent.start()
            except asyncio.CancelledError:
                break
            except Exception:
                restarts += 1
                logger.exception(
                    "Agent %s crashed (restart %d/%d)",
                    agent_id, restarts, max_restarts,
                )
                if restarts <= max_restarts and self._running:
                    logger.info("Restarting agent %s in %ds...", agent_id, restart_delay)
                    await asyncio.sleep(restart_delay)
                    if hasattr(agent.cli, "resume"):
                        try:
                            await agent.cli.resume()
                            logger.info("Resumed CLI session for %s", agent_id)
                        except Exception:
                            logger.warning("Could not resume session for %s, starting fresh", agent_id)
                else:
                    logger.error("Agent %s exceeded max restarts", agent_id)

    async def _heartbeat_loop(self) -> None:
        """Write orchestrator heartbeat to Redis with auto-expiring TTL."""

        interval = self.config.system.heartbeat_interval
        ttl = interval * 3
        consecutive_failures = 0
        while self._running:
            try:
                await self.bus.redis.set(
                    "orchestrator:heartbeat", str(time.time()), ex=ttl,
                )
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    logger.warning("Heartbeat write failed %d consecutive times", consecutive_failures, exc_info=True)
                else:
                    logger.debug("Heartbeat write failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _cleanup_thread_branch(self, thread_id: str) -> None:
        """Delete the agent branch for a terminated thread and clear Redis mapping."""
        try:
            branch = await self.bus.redis.hget("orchestrator:thread_branches", thread_id)
            if not branch:
                return
            base_dir = self.config.system.working_dir
            # Check no worktree has this branch checked out
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=base_dir, capture_output=True, text=True,
            )
            if f"branch refs/heads/{branch}" not in (result.stdout or ""):
                subprocess.run(["git", "branch", "-D", branch], cwd=base_dir, capture_output=True)
                logger.info("Deleted branch %s for thread %s", branch, thread_id[:8])
            await self.bus.redis.hdel("orchestrator:thread_branches", thread_id)
        except Exception:
            logger.debug("Branch cleanup failed for thread %s", thread_id[:8], exc_info=True)

    async def _cleanup_completed_branches(self) -> None:
        """Delete branches for threads that completed or were abandoned in previous runs."""
        try:
            branches = await self.bus.redis.hgetall("orchestrator:thread_branches")
        except Exception:
            return
        if not branches:
            return
        active_threads: set[str] = set()
        for stage in self._ACTIVE_WORK_STAGES:
            try:
                members = await self.bus.redis.smembers(f"orchestrator:active:{stage}")
                active_threads.update(members)
            except Exception:
                pass
        base_dir = self.config.system.working_dir
        cleaned = 0
        for tid, branch in branches.items():
            if tid in active_threads:
                continue
            subprocess.run(["git", "branch", "-D", branch], cwd=base_dir, capture_output=True)
            try:
                await self.bus.redis.hdel("orchestrator:thread_branches", tid)
            except Exception:
                pass
            cleaned += 1
        if cleaned:
            logger.info("Startup: cleaned %d branches from completed/abandoned threads", cleaned)

    def _cleanup_stale_worktrees(self) -> None:
        """Remove orchestrator-owned worktree dirs from previous crashed runs."""
        base_dir = self.config.system.working_dir
        wt_root = Path(base_dir).resolve() / ".worktrees"
        subprocess.run(["git", "worktree", "prune"], cwd=base_dir, capture_output=True)
        if not wt_root.exists():
            return
        cleaned = 0
        failed = 0
        for d in wt_root.iterdir():
            if not d.is_dir():
                continue
            name = d.name
            if not (name.startswith("developer-") or name.startswith("reviewer-")):
                continue
            # force=True: startup cleanup is crash recovery, and the tree is garbage.
            if remove_worktree(base_dir, d, force=True):
                cleaned += 1
            else:
                failed += 1
        if cleaned:
            logger.info("Startup: cleaned %d stale worktrees from previous run", cleaned)
        if failed:
            # Previously this path could not report anything. It now can, and must:
            # a worktree that survives cleanup is disk that never comes back.
            logger.error(
                "Startup: %d stale worktree(s) could NOT be removed and will accumulate. "
                "Remove them by hand.", failed,
            )

    async def _cleanup_stale_agent_keys(self) -> None:
        """Remove agent:* keys from previous runs so the dashboard starts clean."""
        r = self.bus.redis
        removed = 0
        try:
            async for key in r.scan_iter("agent:*"):
                await r.delete(key)
                removed += 1
            if removed:
                logger.info("Cleaned up %d stale agent keys from previous run", removed)
        except Exception:
            logger.debug("Agent key cleanup failed", exc_info=True)

    def _cleanup_worktrees(self) -> None:
        """Remove git worktrees created for parallel developer agents."""
        if not self.config:
            return
        base_dir = self.config.system.working_dir
        worktree_root = Path(base_dir).resolve() / ".worktrees"
        if not worktree_root.exists():
            return
        for d in worktree_root.iterdir():
            if d.is_dir():
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(d)],
                        cwd=base_dir, capture_output=True,
                    )
                    logger.info("Removed worktree %s", d.name)
                except Exception:
                    pass
        try:
            shutil.rmtree(worktree_root, ignore_errors=True)
        except Exception:
            pass

    async def _cleanup_own_agent_keys(self) -> None:
        """Remove agent keys for agents we manage, on shutdown."""
        r = self.bus.redis
        for aid in self.agents:
            for suffix in ("status", "heartbeat", "busy_since", "current_task", "paused"):
                try:
                    await r.delete(f"agent:{aid}:{suffix}")
                except Exception:
                    pass

    async def shutdown(self) -> None:
        if not self._running:
            return

        logger.info("Shutting down orchestrator...")
        self._running = False

        for aid, agent in self.agents.items():
            try:
                await agent.stop()
                logger.info("Stopped agent %s", aid)
            except Exception:
                logger.exception("Error stopping agent %s", aid)

        for task in self._agent_tasks.values():
            if not task.done():
                task.cancel()

        pending = [t for t in self._agent_tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Clean up git worktrees created for parallel developers
        self._cleanup_worktrees()

        if self.bus:
            await self._cleanup_own_agent_keys()
            await self.bus.close()

        logger.info("Orchestrator shutdown complete")

    # ── One-shot mode ───────────────────────────────────────

    async def start_once(self) -> None:
        self.load_config()
        await run_preflight(self.config)
        self._safety = self._build_safety()

        max_tasks = self.config.system.max_concurrent_tasks
        if max_tasks > 0:
            self._task_semaphore = asyncio.Semaphore(max_tasks)

        redis_url = self.config.system.redis_url
        self.bus = MessageBus(redis_url, stream_maxlen=self.config.system.stream_maxlen)
        await self.bus.connect()

        self._running = True
        self._spawn_agents()

        logger.info("One-shot mode: cascading pipeline")

        await asyncio.sleep(2)
        await self._publish_startup_triggers()

        pr_task = None
        if self.config.system.enable_prs:
            pr_task = asyncio.create_task(self._pr_creation_loop())

        timeout = self.config.system.once_timeout
        try:
            await asyncio.wait_for(
                self._track_pipeline_completion(),
                timeout=timeout,
            )
            await asyncio.sleep(5)
            logger.info("One-shot pipeline completed successfully.")
        except asyncio.TimeoutError:
            logger.warning("One-shot pipeline timed out after %ds.", timeout)
        finally:
            if pr_task:
                pr_task.cancel()
            await self.shutdown()

    async def _track_pipeline_completion(self) -> None:
        proposals_seen: set[str] = set()
        resolved: set[str] = set()
        change_rounds: dict[str, int] = {}
        max_change_rounds = self.config.system.max_change_rounds
        idle_timeout = self.config.system.idle_timeout

        watch_channels = ["proposals", "reviews", "review-results"]

        while True:
            block_timeout = idle_timeout if resolved else None

            got_message = False
            try:
                if block_timeout:
                    envelope = await asyncio.wait_for(
                        self._next_message(watch_channels),
                        timeout=block_timeout,
                    )
                else:
                    envelope = await self._next_message(watch_channels)
                got_message = True
            except asyncio.TimeoutError:
                logger.info(
                    "Pipeline idle for %ds with %d/%d resolved. Completing.",
                    idle_timeout, len(resolved), len(proposals_seen),
                )
                return

            if not got_message:
                continue

            if envelope.message_type == MessageType.PROPOSAL:
                proposals_seen.add(envelope.thread_id)
                logger.info("Tracking proposal %s (%d total)", envelope.thread_id[:8], len(proposals_seen))

            if envelope.message_type == MessageType.PROPOSAL_REVIEW:
                if envelope.payload.get("decision") == "rejected":
                    resolved.add(envelope.thread_id)
                    logger.info("Proposal %s rejected (%d/%d)", envelope.thread_id[:8], len(resolved), len(proposals_seen))

            if envelope.message_type == MessageType.REVIEW_RESULT:
                decision = envelope.payload.get("decision", "")
                tid = envelope.thread_id

                if decision == "approved":
                    resolved.add(tid)
                    logger.info("Proposal %s completed (%d/%d)", tid[:8], len(resolved), len(proposals_seen))
                elif decision == "changes_requested":
                    change_rounds[tid] = change_rounds.get(tid, 0) + 1
                    rounds = change_rounds[tid]
                    logger.info("Proposal %s: changes_requested round %d/%d", tid[:8], rounds, max_change_rounds)
                    if rounds >= max_change_rounds:
                        resolved.add(tid)
                        logger.warning("Proposal %s exhausted %d change rounds", tid[:8], max_change_rounds)

            if proposals_seen and resolved >= proposals_seen:
                logger.info("All %d proposals resolved.", len(proposals_seen))
                return

    async def _next_message(self, channels: list[str]) -> Envelope:
        if not hasattr(self, "_pipeline_sub"):
            new_only = {ch: "$" for ch in channels}
            self._pipeline_sub = self.bus.subscribe_simple(
                channels, last_ids=new_only
            ).__aiter__()
        return await self._pipeline_sub.__anext__()

    @property
    def task_semaphore(self) -> asyncio.Semaphore | None:
        return self._task_semaphore
