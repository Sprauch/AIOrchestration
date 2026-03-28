"""Textual TUI monitor for the agent system.

Tab-based dashboard:
  [1] Overview  — agent status table + pipeline snapshot + recent activity
  [2] Events    — full scrolling log of pipeline events
  [3] Traces    — CLI prompt/response with collapsible full content (Enter to expand)
  [4] Redis     — raw stream data showing everything flowing through pipes
  [5] Metrics   — live counters from orchestrator:metrics
  [6] Threads   — drill into a single proposal end-to-end
  [7] Approve   — approve/deny human gates (flashes when gates arrive)

Press 1-7 or click tabs. Arrow keys to scroll. Enter to expand/collapse traces.
In Threads tab: select a row to see the full timeline.
In Approve tab: select a gate, press [a] to approve or [d] to deny.
Press [p] to pause/resume the selected agent. Press [P] to pause/resume all.
Press q to quit.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import (
    Header, Footer, Static, TabbedContent, TabPane,
    DataTable, RichLog, Collapsible,
)
from textual.binding import Binding
from rich.text import Text

from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus
from agents.core.metrics import METRICS_KEY
from agents.core.state import load_snapshot_from_connection

# ── Styles ──────────────────────────────────────────────────

ROLE_ICONS = {
    "pm": "PM", "architect": "ARCH", "developer": "DEV",
    "reviewer": "REV", "system": "SYS", "human": "HMN",
    "challenger": "CHAL",
}
ROLE_COLORS = {
    "pm": "blue", "architect": "yellow", "developer": "green",
    "reviewer": "magenta", "system": "white", "human": "cyan",
    "challenger": "red",
}


def _agent_role(agent_id: str) -> str:
    if agent_id.endswith("-challenger"):
        return "challenger"
    return agent_id.rsplit("-", 1)[0] if "-" in agent_id else agent_id


STATUS_DISPLAY = {
    "active": ("*", "green"), "busy": ("~", "yellow"), "stopped": ("-", "red"),
}
DECISION_DISPLAY = {
    "approved": ("+", "green"), "rejected": ("x", "red"),
    "needs_revision": ("~", "yellow"), "changes_requested": ("~", "yellow"),
    "completed": ("+", "green"), "in_progress": ("~", "yellow"),
    "blocked": ("!", "red"), "failed": ("x", "red"),
}

ALL_CHANNELS = [
    "proposals", "reviews", "tasks", "review-requests",
    "review-results", "progress", "human-gates", "cli-traces", "system",
]

CHANNEL_FOR_TYPE = {
    "proposal": "proposals", "codebase_analysis": "proposals",
    "proposal_review": "reviews", "task_assignment": "tasks",
    "task_progress": "progress", "review_request": "review-requests",
    "review_result": "review-results", "cli_trace": "cli-traces",
    "human_gate": "human-gates", "system": "system",
}


class MonitorApp(App):
    TITLE = "Agent Team Monitor"
    CSS = """
    Screen { background: $surface; }
    #overview-top { height: auto; max-height: 26; }
    #agent-table { height: auto; max-height: 12; }
    #pipeline-bar { height: 3; padding: 0 1; background: $panel; }
    #stream-counts { height: 2; padding: 0 1; }
    #attention-block { height: auto; min-height: 4; max-height: 14; padding: 1; background: $panel; }
    #recent-activity { height: 1fr; }
    .log-panel { height: 1fr; }
    #traces-scroll { height: 1fr; }
    .trace-entry { margin-bottom: 1; }
    .trace-content { padding: 0 2; }
    Collapsible { padding: 0; margin: 0; }
    #threads-table { height: auto; max-height: 12; }
    #thread-timeline { height: 1fr; }
    #approve-table { height: auto; max-height: 14; }
    #approve-detail { height: 1fr; }
    .approve-flash { color: $error; text-style: bold; }
    """

    BINDINGS = [
        Binding("1", "switch_tab('overview')", "Overview", show=True),
        Binding("2", "switch_tab('events')", "Events", show=True),
        Binding("3", "switch_tab('traces')", "Traces", show=True),
        Binding("4", "switch_tab('redis')", "Redis", show=True),
        Binding("5", "switch_tab('metrics')", "Metrics", show=True),
        Binding("6", "switch_tab('threads')", "Threads", show=True),
        Binding("7", "switch_tab('approve')", "Approve", show=True),
        Binding("a", "approve_gate", "Approve", show=False),
        Binding("d", "deny_gate", "Deny", show=False),
        Binding("p", "toggle_pause_agent", "Pause/Resume", show=True),
        Binding("P", "toggle_pause_all", "Pause/Resume All", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    STALE_AGENT_THRESHOLD = 60  # seconds before an agent heartbeat is "stale"
    STUCK_THREAD_THRESHOLD = 300  # seconds before a thread is "stuck" (no progress)

    def __init__(self, redis_url: str, *,
                 stale_agent_threshold: int | None = None,
                 stuck_thread_threshold: int | None = None):
        super().__init__()
        self.redis_url = redis_url
        if stale_agent_threshold is not None:
            self.STALE_AGENT_THRESHOLD = stale_agent_threshold
        if stuck_thread_threshold is not None:
            self.STUCK_THREAD_THRESHOLD = stuck_thread_threshold
        self.bus = MessageBus(redis_url)
        self.agents: dict[str, dict] = {}
        self.proposals_total = 0
        self.proposals_resolved = 0
        self.stream_counts: dict[str, int] = {ch: 0 for ch in ALL_CHANNELS}
        self.role_message_counts: dict[str, int] = {}
        self.latest_by_channel: dict[str, Envelope] = {}
        self.latest_trace: Envelope | None = None
        self.pending_gates = 0
        self._total_count = 0
        self._trace_count = 0
        self._start_time = time.time()

        # Attention tracking
        self._pending_gate_ids: set[str] = set()  # gate envelope IDs not yet resolved
        self._pending_gates_list: list[Envelope] = []  # full gate envelopes for the Approve tab
        self._handled_gate_ids: set[str] = set()  # gates already approved/denied
        self._thread_last_seen: dict[str, float] = {}  # thread_id -> last message time
        self._thread_labels: dict[str, str] = {}  # thread_id -> short label
        self._resolved_threads: set[str] = set()
        self._pr_failures: list[str] = []  # branch names of failed PRs
        self._changes_requested: dict[str, str] = {}  # thread_id -> branch, awaiting rework

        # Thread drill-down: store all envelopes by thread
        self._thread_events: dict[str, list[Envelope]] = {}  # thread_id -> [envelopes]
        self._selected_thread: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Overview", id="overview"):
                with Vertical(id="overview-top"):
                    yield DataTable(id="agent-table")
                    yield Static(id="pipeline-bar")
                    yield Static(id="stream-counts")
                    yield Static(id="attention-block")
                yield RichLog(id="recent-activity", highlight=True, markup=False, wrap=True, max_lines=200)
            with TabPane("Events", id="events"):
                yield RichLog(id="events-log", highlight=True, markup=False, wrap=True, max_lines=500, classes="log-panel")
            with TabPane("Traces", id="traces"):
                yield VerticalScroll(id="traces-scroll")
            with TabPane("Redis", id="redis"):
                yield RichLog(id="redis-log", highlight=True, markup=False, wrap=True, max_lines=500, classes="log-panel")
            with TabPane("Metrics", id="metrics"):
                yield Static("Loading metrics...", id="metrics-display")
            with TabPane("Threads", id="threads"):
                yield DataTable(id="threads-table")
                yield RichLog(id="thread-timeline", highlight=True, markup=False, wrap=True, max_lines=500)
            with TabPane("Approve", id="approve"):
                yield Static(Text("Select a gate and press [a] to approve or [d] to deny."), id="approve-help")
                yield DataTable(id="approve-table")
                yield RichLog(id="approve-detail", highlight=True, markup=False, wrap=True, max_lines=200)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.add_columns("Agent", "Status", "Heartbeat", "Task", "Last Activity")

        threads_table = self.query_one("#threads-table", DataTable)
        threads_table.add_columns("Thread", "Label", "Status", "Messages", "Last Activity")
        threads_table.cursor_type = "row"

        approve_table = self.query_one("#approve-table", DataTable)
        approve_table.add_columns("Gate", "From", "Action", "Reason")
        approve_table.cursor_type = "row"

        await self.bus.connect()
        await self._load_snapshot()
        self._refresh_pipeline()
        self._refresh_attention()
        await self._replay_history()
        self._refresh_threads_table()
        self._refresh_approve_table()
        await self._refresh_metrics_table()

        self._watch_task = asyncio.create_task(self._watch_messages())
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def on_unmount(self) -> None:
        for attr in ('_watch_task', '_poll_task'):
            if hasattr(self, attr):
                getattr(self, attr).cancel()
        await self.bus.close()

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    # ── Data loading (shared read model) ──────────────────────

    async def _load_snapshot(self) -> None:
        """Load system state via the shared read model."""
        snapshot = await load_snapshot_from_connection(self.bus.redis)

        # Merge agent states, preserving last_activity from message ingestion
        old_activity = {aid: info.get("last_activity", "") for aid, info in self.agents.items()}
        self.agents.clear()
        for a in snapshot.agents:
            self.agents[a.agent_id] = {
                "status": "paused" if a.paused else a.status,
                "heartbeat": str(a.heartbeat) if a.heartbeat else None,
                "current_task": a.current_task,
                "last_activity": old_activity.get(a.agent_id, ""),
                "paused": a.paused,
            }

        for s in snapshot.streams:
            self.stream_counts[s.name] = s.count

        self._cached_metrics = snapshot.metrics
        self._orch_heartbeat = str(snapshot.orchestrator_heartbeat) if snapshot.orchestrator_heartbeat else None

        self._refresh_agent_table()

    async def _replay_history(self) -> None:
        r = self.bus.redis
        all_msgs: list[tuple[str, Envelope]] = []
        for ch in ALL_CHANNELS:
            try:
                messages = await r.xrevrange(f"stream:{ch}", count=500)
                messages.reverse()
                for msg_id, data in messages:
                    try:
                        all_msgs.append((ch, Envelope.from_json(data["data"])))
                    except Exception:
                        continue
            except Exception:
                continue

        all_msgs.sort(key=lambda x: x[1].timestamp)
        for ch, env in all_msgs:
            self._ingest(ch, env, is_replay=True)

        self._refresh_agent_table()
        self._refresh_pipeline()
        self._refresh_attention()

        for log_id in ("recent-activity", "events-log", "redis-log"):
            try:
                self.query_one(f"#{log_id}", RichLog).scroll_end(animate=False)
            except Exception:
                pass
        try:
            self.query_one("#traces-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    # ── Live watchers ───────────────────────────────────────

    async def _watch_messages(self) -> None:
        new_only = {ch: "$" for ch in ALL_CHANNELS}
        async for envelope in self.bus.subscribe_simple(ALL_CHANNELS, last_ids=new_only):
            channel = CHANNEL_FOR_TYPE.get(envelope.message_type.value, "system")
            self._ingest(channel, envelope, is_replay=False)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._load_snapshot()
            self._refresh_pipeline()
            self._refresh_attention()
            await self._refresh_metrics_table()

    # ── Message routing ─────────────────────────────────────

    def _ingest(self, channel: str, env: Envelope, is_replay: bool = False) -> None:
        self._total_count += 1
        self.stream_counts[channel] = self.stream_counts.get(channel, 0) + 1
        self.role_message_counts[env.sender_role] = self.role_message_counts.get(env.sender_role, 0) + 1
        self.latest_by_channel[channel] = env

        # Parse timestamp for thread tracking
        try:
            msg_time = datetime.fromisoformat(env.timestamp).timestamp()
        except Exception:
            msg_time = time.time()

        # Track thread activity and store envelopes for drill-down
        if env.thread_id:
            self._thread_last_seen[env.thread_id] = msg_time
            if env.message_type != MessageType.CLI_TRACE:
                self._thread_events.setdefault(env.thread_id, []).append(env)

        if env.message_type == MessageType.PROPOSAL:
            self.proposals_total += 1
            self._thread_labels[env.thread_id] = env.payload.get("title", env.thread_id[:8])
        elif env.message_type == MessageType.PROPOSAL_REVIEW:
            decision = env.payload.get("decision", "")
            if decision == "rejected":
                self.proposals_resolved += 1
                self._resolved_threads.add(env.thread_id)
        elif env.message_type == MessageType.TASK_ASSIGNMENT:
            branch = env.payload.get("branch_name", "")
            if branch:
                self._thread_labels[env.thread_id] = branch
        elif env.message_type == MessageType.REVIEW_RESULT:
            decision = env.payload.get("decision", "")
            if decision == "approved":
                self.proposals_resolved += 1
                self._resolved_threads.add(env.thread_id)
                self._changes_requested.pop(env.thread_id, None)
            elif decision == "changes_requested":
                branch = env.payload.get("branch_name", self._thread_labels.get(env.thread_id, env.thread_id[:8]))
                self._changes_requested[env.thread_id] = branch
        elif env.message_type == MessageType.TASK_PROGRESS:
            # Developer made progress — clear from changes_requested if reworking
            if env.payload.get("status") == "completed":
                self._changes_requested.pop(env.thread_id, None)
        elif env.message_type == MessageType.HUMAN_GATE:
            self.pending_gates += 1
            self._pending_gate_ids.add(env.id)
            if env.id not in self._handled_gate_ids:
                self._pending_gates_list.append(env)
                if not is_replay:
                    self._flash_approve_tab()
        elif env.message_type == MessageType.SYSTEM:
            action = env.payload.get("action", "")
            gate_id = env.payload.get("gate_id", "")
            if "approval_granted" in action or "approval_denied" in action:
                self.pending_gates = max(0, self.pending_gates - 1)
                self._pending_gate_ids.discard(gate_id)
                self._handled_gate_ids.add(gate_id)

        if env.sender_id in self.agents:
            self.agents[env.sender_id]["last_activity"] = self._one_liner(env)

        if env.message_type == MessageType.CLI_TRACE:
            self.latest_trace = env
            self._add_trace(env)
        else:
            self._write_log("events-log", self._fmt_event(env, channel))

        if env.message_type != MessageType.CLI_TRACE:
            self._write_log("recent-activity", self._fmt_event(env, channel))

        self._write_log("redis-log", self._fmt_redis(env, channel))

        if not is_replay:
            self._refresh_agent_table()
            self._refresh_pipeline()
            self._refresh_attention()
            self._refresh_threads_table()
            # If the selected thread got a new event, re-render its timeline
            if self._selected_thread and env.thread_id == self._selected_thread:
                self._render_thread_timeline(self._selected_thread)

    def _write_log(self, log_id: str, text: Text) -> None:
        try:
            self.query_one(f"#{log_id}", RichLog).write(text)
        except Exception:
            pass

    # ── Trace entries (collapsible) ─────────────────────────

    def _add_trace(self, env: Envelope) -> None:
        self._trace_count += 1
        p = env.payload
        direction = p.get("direction", "?")
        content = p.get("content", p.get("content_preview", ""))
        length = p.get("content_length", len(content))
        is_error = p.get("is_error", False)
        ts = env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp

        arrow = "->" if direction == "prompt" else "<-"
        err = " ERROR" if is_error else ""
        delib_round = p.get("deliberation_round")
        model_label = p.get("model", "")
        delib_tag = ""
        if delib_round:
            delib_tag = f" [R{delib_round}/{model_label}]"

        formatted = self._format_trace_content(content or "", direction)
        first_line = formatted.split("\n")[0][:70]
        title = f"{ts}  {env.sender_id}  {arrow} {direction.upper()}{err}{delib_tag}  ({length}ch)  {first_line}"

        try:
            scroll = self.query_one("#traces-scroll", VerticalScroll)
            collapsible = Collapsible(
                Static(Text(formatted), classes="trace-content"),
                title=title,
                collapsed=True,
                classes="trace-entry",
            )
            scroll.mount(collapsible)
            scroll.scroll_end(animate=False)
        except Exception:
            pass

    def _format_trace_content(self, content: str, direction: str) -> str:
        """Format trace content for readable display.

        For responses: tries to parse JSON/JSONL and extract meaningful text.
        For prompts: returns as-is (already readable).
        """
        if not content:
            return "(empty)"

        if direction == "prompt":
            return content

        # Try to parse as a single JSON object
        try:
            data = json.loads(content)
            return self._format_json_response(data)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try JSONL (multiple JSON lines — common from Claude stream-json and Codex)
        lines = content.strip().splitlines()
        if lines and lines[0].startswith("{"):
            extracted = self._format_jsonl_response(lines)
            if extracted:
                return extracted

        # Try to find a JSON block inside markdown
        if "```json" in content:
            blocks = re.findall(r"```json\s*(.*?)```", content, re.DOTALL)
            if blocks:
                parts = []
                # Include text before/after JSON blocks too
                remainder = content
                for block in blocks:
                    idx = remainder.find(block)
                    before = remainder[:remainder.find("```json")].strip()
                    if before:
                        parts.append(before)
                    try:
                        parsed = json.loads(block)
                        parts.append(json.dumps(parsed, indent=2, ensure_ascii=False))
                    except json.JSONDecodeError:
                        parts.append(block)
                    remainder = remainder[idx + len(block):]
                after = remainder.replace("```", "").strip()
                if after:
                    parts.append(after)
                return "\n\n".join(parts)

        return content

    def _format_json_response(self, data: dict) -> str:
        """Format a single JSON response object readably."""
        parts = []

        # Common fields to surface
        for key in ("title", "decision", "status", "summary", "description",
                     "reasoning", "approach", "branch_name", "changes_summary"):
            val = data.get(key)
            if val:
                parts.append(f"{key}: {val}")

        # Lists
        for key in ("proposals", "comments", "blocking_issues", "concerns",
                     "acceptance_criteria", "files_changed", "files_to_modify",
                     "affected_files", "tests_added"):
            val = data.get(key)
            if val and isinstance(val, list):
                parts.append(f"\n{key}:")
                for item in val[:10]:
                    if isinstance(item, dict):
                        # Nested object — show key fields
                        label = item.get("title", item.get("comment", item.get("file", "")))
                        parts.append(f"  - {label}" if label else f"  - {json.dumps(item, ensure_ascii=False)[:100]}")
                    else:
                        parts.append(f"  - {item}")

        # Nested objects
        spec = data.get("technical_spec")
        if spec and isinstance(spec, dict):
            parts.append(f"\ntechnical_spec:")
            for k, v in spec.items():
                if isinstance(v, list):
                    parts.append(f"  {k}: {', '.join(str(i) for i in v[:5])}")
                else:
                    parts.append(f"  {k}: {v}")

        if parts:
            return "\n".join(parts)

        # Fallback: pretty-print
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _format_jsonl_response(self, lines: list[str]) -> str:
        """Extract readable text from JSONL output (Claude stream-json or Codex exec)."""
        parts = []
        for line in lines:
            try:
                event = json.loads(line)
                etype = event.get("type", "")

                # Claude stream-json: result event has the final text
                if etype == "result":
                    result = event.get("result", "")
                    if result:
                        parts.append(result)

                # Claude stream-json: assistant message has content blocks
                elif etype == "assistant":
                    message = event.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            parts.append(block["text"])

                # Codex exec: item.completed has the agent message
                elif etype == "item.completed":
                    item = event.get("item", {})
                    text = item.get("text", "")
                    if text:
                        parts.append(text)

            except json.JSONDecodeError:
                continue

        return "\n".join(parts) if parts else ""

    # ── Event formatter ─────────────────────────────────────

    def _fmt_event(self, env: Envelope, channel: str) -> Text:
        role = env.sender_role
        icon = ROLE_ICONS.get(role, "?")
        color = ROLE_COLORS.get(role, "white")
        ts = env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp
        mt = env.message_type.value
        p = env.payload

        t = Text()
        t.append(f"{ts} ", style="dim")
        t.append(f"{icon:<5}", style=color)
        t.append(f"{env.sender_id:<14} ", style=f"bold {color}")

        if mt == "proposal":
            title = p.get("title", "?")[:55]
            pri = p.get("priority", "")
            t.append(f"PROPOSAL: {title}", style="bold white")
            if pri:
                ps = "bold red" if pri <= 2 else "yellow" if pri == 3 else "dim"
                t.append(f" P{pri}", style=ps)
        elif mt == "proposal_review":
            d = p.get("decision", "?")
            di, ds = DECISION_DISPLAY.get(d, ("?", "white"))
            t.append(f"[{di}] {d.upper()}", style=f"bold {ds}")
            reasoning = p.get("reasoning", "")
            if reasoning:
                t.append(f"  {reasoning[:60]}", style="dim")
        elif mt == "task_assignment":
            branch = p.get("branch_name", "?")
            t.append(f"TASK: {branch}", style="bold cyan")
            files = p.get("files_to_modify", [])
            if files:
                t.append(f"  [{', '.join(files[:3])}]", style="dim")
        elif mt == "task_progress":
            s = p.get("status", "?")
            di, ds = DECISION_DISPLAY.get(s, ("?", "white"))
            t.append(f"[{di}] {s}", style=f"bold {ds}")
            summary = p.get("changes_summary", "")
            if summary:
                t.append(f"  {summary[:50]}", style="dim")
        elif mt == "review_request":
            branch = p.get("branch_name", "?")
            tests = p.get("tests_passed", False)
            t.append(f"REVIEW REQ: {branch}", style="cyan")
            t.append(f"  tests:{'pass' if tests else 'FAIL'}", style="green" if tests else "red")
        elif mt == "review_result":
            d = p.get("decision", "?")
            target = p.get("_target_agent_id", "")
            di, ds = DECISION_DISPLAY.get(d, ("?", "white"))
            t.append(f"[{di}] REVIEW {d.upper()}", style=f"bold {ds}")
            if target:
                t.append(f" -> {target}", style="dim")
        elif mt == "human_gate":
            action = p.get("action", "?")
            reason = p.get("reason", "")
            t.append(f"APPROVAL NEEDED: {action}", style="bold red")
            if reason:
                t.append(f"  {reason[:50]}", style="dim")
        elif mt == "system":
            action = p.get("action", "")
            target_role = p.get("target_role", "")
            if action == "trigger_analysis":
                t.append("trigger analysis", style="bold")
                if target_role:
                    t.append(f" -> {target_role}", style="dim")
            elif "approval" in action:
                ai = "[+]" if "granted" in action else "[x]"
                t.append(f"{ai} {action}", style="bold")
            else:
                msg = p.get("message", action)
                t.append(f"{msg[:60]}", style="dim")
        else:
            t.append(f"{mt}: {str(p)[:50]}", style="dim")

        return t

    # ── Redis raw formatter ─────────────────────────────────

    def _fmt_redis(self, env: Envelope, channel: str) -> Text:
        ts = env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp
        color = ROLE_COLORS.get(env.sender_role, "white")

        t = Text()
        t.append(f"{ts} ", style="dim")
        t.append(f"XADD ", style="bold green")
        t.append(f"stream:{channel} ", style="bold cyan")
        t.append(f"id=", style="dim")
        t.append(f"{env.id[:8]}.. ", style="yellow")
        t.append(f"from=", style="dim")
        t.append(f"{env.sender_id} ", style=f"bold {color}")
        t.append(f"type=", style="dim")
        t.append(f"{env.message_type.value} ", style="bold white")

        if env.recipient_role:
            t.append(f"to={env.recipient_role} ", style="bold")
        if env.thread_id:
            t.append(f"thr={env.thread_id[:8]}.. ", style="yellow")

        payload = dict(env.payload)
        payload.pop("_target_agent_id", None)
        if env.message_type == MessageType.CLI_TRACE:
            d = payload.get("direction", "?")
            length = payload.get("content_length", 0)
            t.append(f"  {'>' if d == 'prompt' else '<'} {d} ({length}ch)", style="dim")
        else:
            try:
                ps = json.dumps(payload, ensure_ascii=False)
                if len(ps) > 120:
                    ps = ps[:120] + ".."
                t.append(f"  {ps}", style="dim")
            except Exception:
                t.append(f"  {str(payload)[:120]}", style="dim")

        return t

    # ── Helpers ──────────────────────────────────────────────

    def _one_liner(self, env: Envelope) -> str:
        mt = env.message_type.value
        p = env.payload
        if mt == "proposal":
            return f"PROPOSAL: {p.get('title', '?')[:35]}"
        if mt == "proposal_review":
            return p.get("decision", "?")
        if mt == "task_assignment":
            return f"TASK: {p.get('branch_name', '?')}"
        if mt == "task_progress":
            return p.get("status", "?")
        if mt == "review_request":
            return "review submitted"
        if mt == "review_result":
            return f"review: {p.get('decision', '?')}"
        if mt == "cli_trace":
            d = p.get("direction", "?")
            return f"{d} ({p.get('content_length', 0)}ch)"
        if mt == "system":
            return p.get("action", "?")
        return mt[:20]

    def _refresh_agent_table(self) -> None:
        try:
            table = self.query_one("#agent-table", DataTable)
        except Exception:
            return

        table.clear()

        # Use cached heartbeat from async poll (set in _poll_loop via snapshot)
        orch_hb = getattr(self, "_orch_heartbeat", None)

        if orch_hb:
            age = int(time.time() - float(orch_hb))
            hb_str = f"{age}s" if age < 60 else f"{age // 60}m"
            hb_style = "green" if age < 30 else "red"
        else:
            hb_str = "-"
            hb_style = "dim"

        table.add_row(
            Text("SYS  orchestrator", style="bold white"),
            Text("* service" if orch_hb else "- unknown", style=hb_style),
            Text(hb_str, style=hb_style),
            Text("-", style="dim"),
            Text("-"),
        )

        for agent_id in sorted(self.agents.keys()):
            info = self.agents[agent_id]
            role = _agent_role(agent_id)
            icon = ROLE_ICONS.get(role, "?")
            status = info.get("status", "unknown")
            s_icon, s_color = STATUS_DISPLAY.get(status, ("?", "white"))

            hb = info.get("heartbeat")
            if hb:
                age = int(time.time() - float(hb))
                hb_str = f"{age}s" if age < 60 else f"{age // 60}m"
            else:
                hb_str = "-"

            task = info.get("current_task", "")
            last = info.get("last_activity", "-")

            table.add_row(
                Text(f"{icon:<5}{agent_id}", style=f"bold {ROLE_COLORS.get(role, 'white')}"),
                Text(f"{s_icon} {status}", style=s_color),
                Text(hb_str),
                Text(task[:16] if task else "-", style="dim"),
                Text(str(last)[:40]),
            )

    def _refresh_pipeline(self) -> None:
        total = self.proposals_total
        resolved = self.proposals_resolved

        if total > 0:
            pct = int((resolved / total) * 100)
            w = 30
            filled = int(w * resolved / total)
            bar = "#" * filled + "-" * (w - filled)
            text = f"Proposals {resolved}/{total}  [{bar}] {pct}%"
        else:
            text = "Waiting for proposals..."

        elapsed = int(time.time() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        text += f"    {self._total_count} msgs ({self._trace_count} traces)  {mins:02d}:{secs:02d}"

        try:
            self.query_one("#pipeline-bar", Static).update(text)
        except Exception:
            pass

        parts = [f"{ch}:{self.stream_counts.get(ch, 0)}" for ch in ALL_CHANNELS]
        try:
            self.query_one("#stream-counts", Static).update("  ".join(parts))
        except Exception:
            pass

    def _refresh_attention(self) -> None:
        """Build the Attention block — surfaces what needs operator action."""
        now = time.time()
        items: list[str] = []

        # Stale agents (heartbeat older than threshold)
        for agent_id, info in self.agents.items():
            hb = info.get("heartbeat")
            if hb:
                age = now - float(hb)
                if age > self.STALE_AGENT_THRESHOLD:
                    items.append(f"  [!] STALE: {agent_id} — no heartbeat for {int(age)}s")
            elif info.get("status") not in ("stopped", "unknown"):
                items.append(f"  [!] STALE: {agent_id} — never sent heartbeat")

        # Orchestrator heartbeat stale
        orch_hb = getattr(self, "_orch_heartbeat", None)
        if orch_hb:
            age = now - float(orch_hb)
            if age > 30:
                items.append(f"  [!] STALE: orchestrator — heartbeat {int(age)}s old")
        elif self._total_count > 0:
            items.append(f"  [!] STALE: orchestrator — no heartbeat detected")

        # Pending human gates
        if self._pending_gate_ids:
            items.append(f"  [?] WAITING: {len(self._pending_gate_ids)} pending approval gate(s) — run `agent-orchestrator approve`")

        # Stuck threads (active, not resolved, no message for N minutes)
        for tid, last_seen in self._thread_last_seen.items():
            if tid in self._resolved_threads:
                continue
            idle = now - last_seen
            if idle > self.STUCK_THREAD_THRESHOLD:
                label = self._thread_labels.get(tid, tid[:8])
                mins = int(idle / 60)
                items.append(f"  [~] STUCK: {label} — no progress for {mins}m")

        # Changes requested — awaiting developer rework
        for tid, branch in self._changes_requested.items():
            items.append(f"  [~] REWORK: {branch} — reviewer requested changes")

        # PR failures (from metrics)
        try:
            metrics = getattr(self, "_cached_metrics", {})
            pr_failed = int(metrics.get("prs:failed", 0))
            if pr_failed > 0:
                items.append(f"  [x] FAILED: {pr_failed} PR creation(s) failed — check logs")
        except Exception:
            pass

        # Build display
        if items:
            header = f"Attention ({len(items)})"
            text = header + "\n" + "\n".join(items)
        else:
            text = "Attention: all clear"

        try:
            self.query_one("#attention-block", Static).update(text)
        except Exception:
            pass

    def _refresh_threads_table(self) -> None:
        """Update the threads list table."""
        try:
            table = self.query_one("#threads-table", DataTable)
        except Exception:
            return

        table.clear()

        # Build thread summaries sorted by last activity (most recent first)
        thread_ids = sorted(
            self._thread_events.keys(),
            key=lambda t: self._thread_last_seen.get(t, 0),
            reverse=True,
        )

        for tid in thread_ids:
            events = self._thread_events[tid]
            label = self._thread_labels.get(tid, tid[:8])
            count = len(events)

            # Determine status from the latest meaningful event
            status = self._thread_status(tid, events)

            last_ts = ""
            if events:
                last_env = events[-1]
                last_ts = last_env.timestamp[11:19] if len(last_env.timestamp) > 19 else last_env.timestamp

            status_style = {
                "proposed": "blue",
                "approved": "green",
                "rejected": "red",
                "in_progress": "yellow",
                "review_submitted": "cyan",
                "changes_requested": "yellow",
                "completed": "green",
                "awaiting_gate": "red",
            }.get(status, "dim")

            table.add_row(
                Text(tid[:8], style="dim"),
                Text(str(label)[:40], style="bold"),
                Text(status, style=status_style),
                Text(str(count)),
                Text(last_ts),
                key=tid,
            )

    def _thread_status(self, tid: str, events: list[Envelope]) -> str:
        """Derive the current pipeline status of a thread from its events."""
        if tid in self._resolved_threads:
            # Check if it was approved or rejected
            for env in reversed(events):
                if env.message_type == MessageType.REVIEW_RESULT:
                    if env.payload.get("decision") == "approved":
                        return "completed"
                if env.message_type == MessageType.PROPOSAL_REVIEW:
                    if env.payload.get("decision") == "rejected":
                        return "rejected"
            return "completed"

        if tid in self._changes_requested:
            return "changes_requested"

        if tid in self._pending_gate_ids:
            return "awaiting_gate"

        # Walk backwards to find the latest stage
        for env in reversed(events):
            mt = env.message_type
            if mt == MessageType.REVIEW_RESULT:
                return "changes_requested"  # if not resolved, it's a change request
            if mt == MessageType.REVIEW_REQUEST:
                return "review_submitted"
            if mt == MessageType.TASK_PROGRESS:
                return "in_progress"
            if mt == MessageType.TASK_ASSIGNMENT:
                return "in_progress"
            if mt == MessageType.PROPOSAL_REVIEW:
                d = env.payload.get("decision", "")
                if d == "approved":
                    return "approved"
                if d == "needs_revision":
                    return "needs_revision"
            if mt == MessageType.PROPOSAL:
                return "proposed"

        return "unknown"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in threads table and approve table."""
        if event.data_table.id == "threads-table":
            tid = str(event.row_key.value) if event.row_key else None
            if tid and tid in self._thread_events:
                self._selected_thread = tid
                self._render_thread_timeline(tid)

        elif event.data_table.id == "approve-table":
            gate = self._get_selected_gate()
            if gate:
                self._render_gate_detail(gate)

    def _render_gate_detail(self, gate: Envelope) -> None:
        """Show full details for a gate in the approve-detail log."""
        try:
            log = self.query_one("#approve-detail", RichLog)
        except Exception:
            return

        log.clear()

        p = gate.payload
        ts = gate.timestamp[11:19] if len(gate.timestamp) > 19 else gate.timestamp

        header = Text()
        header.append("APPROVAL REQUIRED", style="bold red")
        log.write(header)
        log.write(Text("=" * 50, style="dim"))

        self._timeline_detail(log, "Gate ID", gate.id[:8])
        self._timeline_detail(log, "Time", ts)
        self._timeline_detail(log, "From", f"{gate.sender_id} ({gate.sender_role})")
        self._timeline_detail(log, "Thread", gate.thread_id[:8])
        label = self._thread_labels.get(gate.thread_id, "")
        if label:
            self._timeline_detail(log, "Thread label", label)
        self._timeline_detail(log, "Action", p.get("action", "?"))
        self._timeline_detail(log, "Reason", p.get("reason", ""))
        self._timeline_detail(log, "Context", p.get("context", ""))

        log.write(Text(""))
        log.write(Text("Press [a] to APPROVE or [d] to DENY", style="bold yellow"))

    def _render_thread_timeline(self, tid: str) -> None:
        """Render the full timeline for a thread into the thread-timeline log."""
        try:
            log = self.query_one("#thread-timeline", RichLog)
        except Exception:
            return

        log.clear()

        events = self._thread_events.get(tid, [])
        label = self._thread_labels.get(tid, tid[:8])
        status = self._thread_status(tid, events)

        # Header
        header = Text()
        header.append(f"Thread {tid[:8]}  ", style="bold")
        header.append(f"{label}  ", style="bold cyan")
        header.append(f"[{status}]  ", style="bold yellow")
        header.append(f"{len(events)} events", style="dim")
        log.write(header)
        log.write(Text("=" * 60, style="dim"))

        # Pipeline stage labels for readable output
        stage_labels = {
            MessageType.SYSTEM: "TRIGGER",
            MessageType.PROPOSAL: "PROPOSAL",
            MessageType.PROPOSAL_REVIEW: "ARCHITECT",
            MessageType.TASK_ASSIGNMENT: "TASK",
            MessageType.TASK_PROGRESS: "PROGRESS",
            MessageType.REVIEW_REQUEST: "REVIEW REQ",
            MessageType.REVIEW_RESULT: "REVIEW",
            MessageType.HUMAN_GATE: "GATE",
        }

        for env in events:
            ts = env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp
            role = env.sender_role
            color = ROLE_COLORS.get(role, "white")
            icon = ROLE_ICONS.get(role, "?")
            stage = stage_labels.get(env.message_type, env.message_type.value.upper())

            # Stage line
            line = Text()
            line.append(f"{ts} ", style="dim")
            line.append(f"{stage:<12}", style="bold white")
            line.append(f"{icon} {env.sender_id}", style=f"bold {color}")
            log.write(line)

            # Details based on message type
            p = env.payload
            mt = env.message_type

            if mt == MessageType.PROPOSAL:
                self._timeline_detail(log, "Title", p.get("title", "?"))
                self._timeline_detail(log, "Priority", str(p.get("priority", "?")))
                self._timeline_detail(log, "Category", p.get("category", "?"))
                self._timeline_detail(log, "Effort", p.get("estimated_effort", "?"))
                files = p.get("affected_files", [])
                if files:
                    self._timeline_detail(log, "Files", ", ".join(files[:5]))
                desc = p.get("description", "")
                if desc:
                    self._timeline_detail(log, "Description", desc[:200])

            elif mt == MessageType.PROPOSAL_REVIEW:
                decision = p.get("decision", "?")
                d_style = "green" if decision == "approved" else "red" if decision == "rejected" else "yellow"
                detail = Text()
                detail.append(f"  Decision: ", style="dim")
                detail.append(decision.upper(), style=f"bold {d_style}")
                log.write(detail)
                reasoning = p.get("reasoning", "")
                if reasoning:
                    self._timeline_detail(log, "Reasoning", reasoning[:200])
                concerns = p.get("concerns", [])
                for c in concerns[:3]:
                    self._timeline_detail(log, "Concern", c)

            elif mt == MessageType.TASK_ASSIGNMENT:
                self._timeline_detail(log, "Branch", p.get("branch_name", "?"))
                self._timeline_detail(log, "Approach", p.get("approach", "")[:150])
                files = p.get("files_to_modify", [])
                if files:
                    self._timeline_detail(log, "Files", ", ".join(files[:5]))
                criteria = p.get("acceptance_criteria", [])
                for c in criteria[:3]:
                    self._timeline_detail(log, "Criteria", c)

            elif mt == MessageType.TASK_PROGRESS:
                s = p.get("status", "?")
                s_style = "green" if s == "completed" else "yellow" if s == "in_progress" else "red"
                detail = Text()
                detail.append(f"  Status: ", style="dim")
                detail.append(s, style=f"bold {s_style}")
                log.write(detail)
                summary = p.get("changes_summary", "")
                if summary:
                    self._timeline_detail(log, "Summary", summary[:200])

            elif mt == MessageType.REVIEW_REQUEST:
                self._timeline_detail(log, "Branch", p.get("branch_name", "?"))
                tests = p.get("tests_passed", False)
                detail = Text()
                detail.append(f"  Tests: ", style="dim")
                detail.append("pass" if tests else "FAIL", style="green" if tests else "bold red")
                log.write(detail)
                files = p.get("files_changed", [])
                if files:
                    self._timeline_detail(log, "Changed", ", ".join(files[:5]))

            elif mt == MessageType.REVIEW_RESULT:
                decision = p.get("decision", "?")
                d_style = "green" if decision == "approved" else "yellow"
                detail = Text()
                detail.append(f"  Decision: ", style="dim")
                detail.append(decision.upper(), style=f"bold {d_style}")
                log.write(detail)
                summary = p.get("summary", "")
                if summary:
                    self._timeline_detail(log, "Summary", summary[:200])
                blocking = p.get("blocking_issues", [])
                for b in blocking[:3]:
                    self._timeline_detail(log, "Blocking", b)

            elif mt == MessageType.HUMAN_GATE:
                self._timeline_detail(log, "Action", p.get("action", "?"))
                self._timeline_detail(log, "Reason", p.get("reason", ""))

            elif mt == MessageType.SYSTEM:
                action = p.get("action", "")
                if "approval" in action:
                    d_style = "green" if "granted" in action else "red"
                    detail = Text()
                    detail.append(f"  ", style="dim")
                    detail.append(action, style=f"bold {d_style}")
                    log.write(detail)
                else:
                    self._timeline_detail(log, "Action", action)

            log.write(Text(""))  # blank separator

    def _timeline_detail(self, log: RichLog, label: str, value: str) -> None:
        if not value:
            return
        line = Text()
        line.append(f"  {label}: ", style="dim")
        line.append(value)
        log.write(line)

    # ── Approval tab ──────────────────────────────────────────

    def _flash_approve_tab(self) -> None:
        """Update the Approve tab label to indicate pending gates."""
        pending = [g for g in self._pending_gates_list if g.id not in self._handled_gate_ids]
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            pane = tabs.get_tab("approve")
            if pending:
                pane.label = f"!! Approve ({len(pending)}) !!"
            else:
                pane.label = "Approve"
        except Exception:
            pass

    def _refresh_approve_table(self) -> None:
        """Update the approve table with pending gates."""
        try:
            table = self.query_one("#approve-table", DataTable)
        except Exception:
            return

        table.clear()

        pending = [g for g in self._pending_gates_list if g.id not in self._handled_gate_ids]
        for gate in pending:
            p = gate.payload
            table.add_row(
                Text(gate.id[:8], style="dim"),
                Text(f"{gate.sender_id} ({gate.sender_role})", style="bold"),
                Text(p.get("action", "?"), style="yellow"),
                Text(p.get("reason", "")[:40]),
                key=gate.id,
            )

        if not pending:
            self._flash_approve_tab()  # clear the flash

    def _get_selected_gate(self) -> Envelope | None:
        """Get the gate envelope for the currently selected row in the approve table."""
        try:
            table = self.query_one("#approve-table", DataTable)
            row_key = table.cursor_row
            if row_key is None:
                return None
            # Get the key from the row
            keys = list(table.rows.keys())
            if row_key < len(keys):
                gate_id = str(keys[row_key].value)
                for gate in self._pending_gates_list:
                    if gate.id == gate_id:
                        return gate
        except Exception:
            pass
        return None

    async def _respond_to_gate(self, gate: Envelope, action: str) -> None:
        """Publish approval response — same logic as the standalone console."""
        response = Envelope(
            sender_id="human",
            sender_role="human",
            message_type=MessageType.SYSTEM,
            payload={"action": action, "gate_id": gate.id},
            thread_id=gate.thread_id,
        )

        # Per-gate channel — the agent blocks on this
        gate_channel = f"gate-responses:{gate.id}"
        await self.bus.publish(gate_channel, response)

        # System stream — audit
        await self.bus.publish("system", response)

        self._handled_gate_ids.add(gate.id)
        self._pending_gate_ids.discard(gate.id)
        self.pending_gates = max(0, self.pending_gates - 1)

        # Update UI
        self._refresh_approve_table()
        self._refresh_attention()

        # Log it
        decision = "APPROVED" if "granted" in action else "DENIED"
        try:
            log = self.query_one("#approve-detail", RichLog)
            line = Text()
            line.append(f"{decision}: ", style="bold green" if "granted" in action else "bold red")
            line.append(f"gate {gate.id[:8]} ", style="dim")
            line.append(f"({gate.payload.get('action', '?')})", style="dim")
            log.write(line)
        except Exception:
            pass

    async def action_toggle_pause_agent(self) -> None:
        """Toggle pause on the selected agent in the Overview table (keybinding: p)."""
        try:
            table = self.query_one("#agent-table", DataTable)
        except Exception:
            self.notify("No agent table found", severity="warning")
            return
        row_key = table.cursor_row
        agent_ids = sorted(self.agents.keys())
        # Row 0 is the orchestrator header, agents start at row 1
        idx = row_key - 1
        if idx < 0 or idx >= len(agent_ids):
            self.notify("Select an agent row first", severity="warning")
            return
        agent_id = agent_ids[idx]
        paused = self.agents[agent_id].get("paused", False)
        try:
            if paused:
                await self.bus.redis.delete(f"agent:{agent_id}:paused")
                self.agents[agent_id]["paused"] = False
                self.notify(f"Resumed {agent_id}")
            else:
                await self.bus.redis.set(f"agent:{agent_id}:paused", "1")
                self.agents[agent_id]["paused"] = True
                self.notify(f"Paused {agent_id}")
            self._refresh_agent_table()
        except Exception as e:
            self.notify(f"Pause error: {e}", severity="error")

    async def action_toggle_pause_all(self) -> None:
        """Pause or resume all agents at once (keybinding: P)."""
        if not self.agents:
            self.notify("No agents to pause", severity="warning")
            return
        # If any agent is active, pause all. If all paused, resume all.
        any_active = any(not a.get("paused") for a in self.agents.values())
        try:
            for agent_id in self.agents:
                if any_active:
                    await self.bus.redis.set(f"agent:{agent_id}:paused", "1")
                    self.agents[agent_id]["paused"] = True
                else:
                    await self.bus.redis.delete(f"agent:{agent_id}:paused")
                    self.agents[agent_id]["paused"] = False
            action = "Paused" if any_active else "Resumed"
            self.notify(f"{action} all {len(self.agents)} agents")
            self._refresh_agent_table()
        except Exception as e:
            self.notify(f"Pause-all error: {e}", severity="error")

    async def action_approve_gate(self) -> None:
        """Approve the selected gate (keybinding: a)."""
        gate = self._get_selected_gate()
        if gate:
            await self._respond_to_gate(gate, "approval_granted")

    async def action_deny_gate(self) -> None:
        """Deny the selected gate (keybinding: d)."""
        gate = self._get_selected_gate()
        if gate:
            await self._respond_to_gate(gate, "approval_denied")

    async def _refresh_metrics_table(self) -> None:
        """Update the Metrics tab with grouped sections and rate deltas.

        Uses a Static widget with .update() for reliable atomic rendering.
        """
        try:
            widget = self.query_one("#metrics-display", Static)
        except Exception:
            return

        metrics = getattr(self, "_cached_metrics", {})
        prev = getattr(self, "_prev_metrics", {})

        if not metrics:
            widget.update("No metrics recorded yet.")
            self._prev_metrics = dict(metrics)
            return

        def _val(key: str) -> int:
            return int(metrics.get(key, 0))

        def _delta(key: str) -> str:
            cur = int(metrics.get(key, 0))
            old = int(prev.get(key, 0))
            diff = cur - old
            return f" (+{diff})" if diff > 0 else ""

        def _row(label: str, key: str) -> str:
            val = _val(key)
            delta = _delta(key)
            return f"  {label:<28} {val:>6}{delta}"

        lines = []

        # Throughput
        lines.append("Throughput")
        lines.append(_row("Total messages", "messages:total"))
        for role in ("pm", "architect", "developer", "reviewer"):
            if _val(f"messages:{role}") > 0:
                lines.append(_row(f"  {role}", f"messages:{role}"))
        lines.append("")

        # Failures
        lines.append("Failures")
        lines.append(_row("Total errors", "errors:total"))
        for role in ("pm", "architect", "developer", "reviewer"):
            if _val(f"errors:{role}") > 0:
                lines.append(_row(f"  {role}", f"errors:{role}"))
        lines.append("")

        # Approval Gates
        lines.append("Approval Gates")
        lines.append(_row("Approved", "gates:approved"))
        lines.append(_row("Denied", "gates:denied"))
        lines.append(_row("Timed out", "gates:timeout"))
        lines.append("")

        # PR Automation
        lines.append("PR Automation")
        lines.append(_row("PRs created", "prs:created"))
        lines.append(_row("PRs failed", "prs:failed"))
        lines.append("")

        # Rates
        if prev:
            msg_delta = int(metrics.get("messages:total", 0)) - int(prev.get("messages:total", 0))
            if msg_delta > 0:
                rate = msg_delta / 5  # poll interval
                lines.append("Rates (last interval)")
                lines.append(f"  Messages/sec               {rate:>6.1f}")
                lines.append("")

        # Timestamp
        import datetime
        lines.append(f"Updated: {datetime.datetime.now().strftime('%H:%M:%S')}")

        widget.update("\n".join(lines))
        self._prev_metrics = dict(metrics)


# ── Entry point ─────────────────────────────────────────────

class MonitorUI:
    def __init__(self, redis_url: str, *,
                 stale_agent_threshold: int | None = None,
                 stuck_thread_threshold: int | None = None):
        self.redis_url = redis_url
        self.stale_agent_threshold = stale_agent_threshold
        self.stuck_thread_threshold = stuck_thread_threshold

    async def run(self) -> None:
        app = MonitorApp(
            self.redis_url,
            stale_agent_threshold=self.stale_agent_threshold,
            stuck_thread_threshold=self.stuck_thread_threshold,
        )
        await app.run_async()
