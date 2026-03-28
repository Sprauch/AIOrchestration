"""Web dashboard backend — serves JSON API + static files.

The frontend is plain HTML/CSS/JS in agents/static/.
This module provides the API endpoints and static file serving.

Usage:
    agent-orchestrator web [--port 8081] [--config agents/config.yaml]
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from agents.core.auditor import audit_pipeline
from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus
from agents.core.redis_keys import _active_keys, _clear_active_work_thread
from agents.core.state import derive_current_phase, load_snapshot
from agents.core.thread_guard import THREAD_CYCLES_KEY

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

ALL_CHANNELS = [
    "proposals", "reviews", "tasks", "review-requests",
    "review-results", "progress", "human-gates", "system",
]


class WebDashboard:
    _ACTIVE_WORK_STAGES = ("proposals", "tasks", "reviews")

    def __init__(self, redis_url: str, port: int = 8081, gate_token: str | None = None,
                 idle_threshold: int = 600, max_change_rounds: int = 3, stream_read_limit: int = 500,
                 max_pending_proposals: int = 3, max_pending_tasks: int = 3, max_pending_reviews: int = 5):
        self.redis_url = redis_url
        self.port = port
        self.gate_token = gate_token
        self._idle_threshold = idle_threshold
        self._max_change_rounds = max_change_rounds
        self._stream_read_limit = stream_read_limit
        self._wip_limits = {
            "proposals": max_pending_proposals,
            "tasks": max_pending_tasks,
            "reviews": max_pending_reviews,
        }
        self.bus = MessageBus(redis_url)

    async def start(self) -> None:
        await self.bus.connect()

        app = web.Application()

        # API routes
        app.router.add_get("/api/snapshot", self._handle_snapshot)
        app.router.add_get("/api/events", self._handle_events_sse)
        app.router.add_get("/api/threads", self._handle_threads)
        app.router.add_get("/api/threads/{thread_id}", self._handle_thread_detail)
        app.router.add_get("/api/agents/{agent_id}", self._handle_agent_detail)
        app.router.add_get("/api/streams/{stream_name}", self._handle_stream_messages)
        app.router.add_get("/api/gates", self._handle_gates)
        app.router.add_post("/api/gates/{gate_id}/approve", self._handle_gate_approve)
        app.router.add_post("/api/gates/{gate_id}/deny", self._handle_gate_deny)
        app.router.add_get("/api/prs", self._handle_prs)
        app.router.add_post("/api/prs/{thread_id}/retry", self._handle_pr_retry)
        app.router.add_get("/api/redis", self._handle_redis)
        app.router.add_post("/api/redis/delete", self._handle_redis_delete)
        app.router.add_get("/api/exceptions", self._handle_exceptions)
        app.router.add_post("/api/agents/{agent_id}/pause", self._handle_agent_pause)
        app.router.add_post("/api/agents/{agent_id}/resume", self._handle_agent_resume)
        app.router.add_post("/api/threads/{thread_id}/reset-cycles", self._handle_thread_reset_cycles)
        app.router.add_post("/api/threads/{thread_id}/abandon", self._handle_thread_abandon)
        app.router.add_get("/api/traces", self._handle_traces)
        app.router.add_get("/api/audit", self._handle_audit)

        # Static files (index.html, app.js, style.css)
        app.router.add_get("/", self._handle_index)
        app.router.add_static("/static", STATIC_DIR)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        logger.info("Web dashboard running at http://localhost:%d", self.port)
        print(f"Web dashboard: http://localhost:{self.port}")

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()
            await self.bus.close()

    # ── Static ─────────────────────────────────────────────

    async def _handle_redis(self, request: web.Request) -> web.Response:
        """Full Redis state dump for the Redis inspector tab."""
        r = self.bus.redis
        result = {"streams": {}, "hashes": {}, "keys": {}}

        # Streams with counts
        for name in ALL_CHANNELS + ["cli-traces"]:
            try:
                count = await r.xlen(f"stream:{name}")
                result["streams"][name] = count
            except Exception:
                result["streams"][name] = 0

        # Hashes
        for hkey in ["orchestrator:metrics", "orchestrator:created_prs", "orchestrator:thread_cycles"]:
            try:
                data = await r.hgetall(hkey)
                result["hashes"][hkey] = dict(data) if data else {}
            except Exception:
                result["hashes"][hkey] = {}

        # Agent keys
        agents = {}
        try:
            async for key in r.scan_iter("agent:*"):
                val = await r.get(key)
                if val is not None:
                    agents[key] = val
        except Exception:
            pass
        result["keys"]["agents"] = agents

        # Active work sets
        active_work = {}
        for stage in self._ACTIVE_WORK_STAGES:
            key = f"orchestrator:active:{stage}"
            try:
                members = await r.smembers(key)
                active_work[key] = sorted(members) if members else []
            except Exception:
                active_work[key] = []
        result["active_work"] = active_work

        # Orchestrator keys
        orch = {}
        for k in ["orchestrator:heartbeat", "orchestrator:schema_version"]:
            try:
                val = await r.get(k)
                if val is not None:
                    orch[k] = val
            except Exception:
                pass
        result["keys"]["orchestrator"] = orch

        return web.json_response(result)

    _ALLOWED_KEY_PREFIXES = ("orchestrator:", "stream:", "agent:", "claim:", "thread:")

    async def _handle_redis_delete(self, request: web.Request) -> web.Response:
        """Delete a specific Redis key or hash field."""
        if err := self._check_gate_auth(request):
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        key = body.get("key", "")
        if not any(key.startswith(p) for p in self._ALLOWED_KEY_PREFIXES):
            return web.json_response(
                {"error": f"Key must start with one of: {', '.join(self._ALLOWED_KEY_PREFIXES)}"},
                status=400,
            )
        field = body.get("field")  # optional — for hash field or set member deletion
        member = body.get("member")  # optional — for set member removal
        r = self.bus.redis

        try:
            if member:
                await r.srem(key, member)
                logger.info("Removed set member %s -> %s", key, member)
            elif field:
                await r.hdel(key, field)
                logger.info("Deleted hash field %s -> %s", key, field)
            else:
                await r.delete(key)
                logger.info("Deleted key %s", key)
            return web.json_response({"status": "deleted", "key": key, "field": field, "member": member})
        except Exception as e:
            return web.json_response({"error": str(e)[:200]}, status=500)

    PR_SET_KEY = "orchestrator:created_prs"

    async def _handle_prs(self, request: web.Request) -> web.Response:
        """Return all PR creation attempts with status."""
        r = self.bus.redis
        prs = []
        try:
            raw = await r.hgetall(self.PR_SET_KEY)
            for thread_id, value in raw.items():
                # value format: "status|timestamp|detail"
                parts = value.split("|", 2)
                status = parts[0] if parts else "unknown"
                timestamp = parts[1] if len(parts) > 1 else ""
                detail = parts[2] if len(parts) > 2 else ""

                # Look up branch name from cached hash (set by architect on task publish)
                branch = ""
                try:
                    branch = await r.hget("orchestrator:thread_branches", thread_id) or ""
                except Exception:
                    pass

                prs.append({
                    "thread_id": thread_id,
                    "status": status,
                    "timestamp": timestamp,
                    "detail": detail,
                    "branch": branch,
                    "failed": "failed" in status.lower(),
                })
        except Exception:
            pass
        return web.json_response(prs)

    async def _handle_pr_retry(self, request: web.Request) -> web.Response:
        """Retry a failed PR by clearing the audit entry and re-publishing the review result."""
        if err := self._check_gate_auth(request):
            return err
        thread_id = request.match_info["thread_id"]
        r = self.bus.redis

        # Delete the failed audit entry so the PR loop will re-process
        try:
            await r.hdel(self.PR_SET_KEY, thread_id)
        except Exception:
            return web.json_response({"error": "Failed to clear PR entry"}, status=500)

        # Synthesize a minimal approved review-result and publish it.
        # This avoids scanning stream history (which can miss older threads).
        try:
            retry_env = Envelope(
                sender_id="web-retry",
                sender_role="system",
                message_type=MessageType.REVIEW_RESULT,
                thread_id=thread_id,
                payload={"decision": "approved", "summary": "PR retry via dashboard"},
            )
            await r.xadd("stream:review-results", {"data": retry_env.to_json()})
            logger.info("Published retry review-result for PR: thread %s", thread_id[:8])
            return web.json_response({"status": "retrying", "thread_id": thread_id})
        except Exception as e:
            return web.json_response({"error": str(e)[:200]}, status=500)

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    # ── API handlers ───────────────────────────────────────

    async def _handle_snapshot(self, request: web.Request) -> web.Response:
        try:
            snapshot = await load_snapshot(self.redis_url)
        except Exception:
            return web.json_response({"error": "Redis unreachable"}, status=503)
        data = asdict(snapshot)

        # Enrich with challenger/deliberation state from recent cli-traces

        challengers = {}  # role -> {"active": bool, "last_seen": timestamp}
        now = time.time()
        try:
            r = self.bus.redis
            traces = await r.xrevrange("stream:cli-traces", count=50)
            for _mid, tdata in traces:
                try:
                    env = Envelope.from_json(tdata["data"])
                    p = env.payload
                    if p.get("model") == "challenger":
                        sender_role = env.sender_role
                        ts = env.timestamp
                        try:
                            from datetime import datetime
                            t = datetime.fromisoformat(ts).timestamp()
                        except Exception:
                            t = 0
                        if sender_role not in challengers or t > challengers[sender_role].get("ts", 0):
                            challengers[sender_role] = {"ts": t, "round": p.get("deliberation_round", 0)}
                except Exception:
                    continue
        except Exception:
            pass

        # Mark which roles have active challengers (seen in last 60s)
        data["challengers"] = {}
        for r, info in challengers.items():
            data["challengers"][r] = {
                "active": (now - info["ts"]) < 60,
                "recent": (now - info["ts"]) < 300,
                "last_round": info["round"],
            }

        # Backpressure / WIP state
        bp = {}
        for stage, limit in self._wip_limits.items():
            try:
                active = int(await self.bus.redis.scard(f"orchestrator:active:{stage}"))
            except Exception:
                active = 0
            bp[stage] = {"active": active, "limit": limit, "gated": active >= limit}
        data["backpressure"] = bp

        # Pipeline phase indicator
        data["pipeline_phase"] = derive_current_phase(snapshot.agents, bp)

        return web.json_response(data, dumps=lambda x: json.dumps(x, default=str))

    async def _handle_events_sse(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        response.content_type = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        await response.prepare(request)

        new_only = {ch: "$" for ch in ALL_CHANNELS}
        try:
            async for envelope in self.bus.subscribe_simple(ALL_CHANNELS, last_ids=new_only):
                if envelope.message_type == MessageType.CLI_TRACE:
                    continue
                event_data = {
                    "id": envelope.id,
                    "type": envelope.message_type.value,
                    "sender": envelope.sender_id,
                    "role": envelope.sender_role,
                    "thread": envelope.thread_id[:8],
                    "timestamp": envelope.timestamp[11:19] if len(envelope.timestamp) > 19 else envelope.timestamp,
                    "payload": envelope.payload,
                }
                await response.write(f"data: {json.dumps(event_data, default=str)}\n\n".encode())
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        return response

    async def _handle_threads(self, request: web.Request) -> web.Response:
        """Enriched thread list with product-facing derived fields."""
        threads = await self._build_thread_model()
        return web.json_response(list(threads.values()))

    async def _handle_exceptions(self, request: web.Request) -> web.Response:
        """Aggregated exceptions: approvals, failed PRs, blocked threads, stale agents."""

        now = time.time()
        result = {"approvals": [], "failed_prs": [], "blocked_threads": [], "stale_agents": []}

        # Pending approvals
        try:
            gates = await (await self._handle_gates(request)).json
            if callable(gates):
                gates_data = await self._get_gates_data()
            else:
                gates_data = []
        except Exception:
            gates_data = await self._get_gates_data()

        result["approvals"] = [g for g in gates_data if g.get("pending")]

        # Failed PRs
        r = self.bus.redis
        try:
            raw = await r.hgetall(self.PR_SET_KEY)
            for tid, value in raw.items():
                if "failed" in value.lower():
                    parts = value.split("|", 2)
                    branch = ""
                    try:
                        branch = await r.hget("orchestrator:thread_branches", tid) or ""
                    except Exception:
                        pass
                    result["failed_prs"].append({
                        "thread_id": tid,
                        "branch": branch,
                        "detail": parts[2] if len(parts) > 2 else "",
                        "timestamp": parts[1] if len(parts) > 1 else "",
                    })
        except Exception:
            pass

        # Blocked/stuck threads — only truly blocked, not normal rework
        threads = await self._build_thread_model()
        for t in threads.values():
            if t["stage"] == "cycle_exhausted":
                result["blocked_threads"].append(t)
            elif t["status"] == "blocked" and t["stage"] != "cycle_exhausted":
                result["blocked_threads"].append(t)
            # Rework threads are normal pipeline state, not exceptions.
            # They only become exceptions if cycle-exhausted.

        # Idle/stale agents — only flag if heartbeat is very old (>10min)
        # Agents legitimately wait minutes between messages; 120s was too aggressive
        try:
            snapshot = await load_snapshot(self.redis_url)
            if snapshot:
                for a in snapshot.agents:
                    if a.heartbeat:
                        age = now - a.heartbeat
                        if age > self._idle_threshold and a.status not in ("stopped", "paused"):
                            result["stale_agents"].append({
                                "agent_id": a.agent_id,
                                "status": a.status,
                                "heartbeat_age": int(age),
                                "threshold": self._idle_threshold,
                            })
        except Exception:
            pass

        return web.json_response(result)

    async def _get_gates_data(self) -> list:
        """Helper to get gates data without going through HTTP.

        Scans stream:system for resolution events (approval_granted,
        approval_denied, gate_timeout).  First event per gate_id wins
        (deterministic outcome precedence).
        """
        _ACTION_TO_OUTCOME = {
            "approval_granted": "approved",
            "approval_denied": "denied",
            "gate_timeout": "timed_out",
        }

        r = self.bus.redis
        # gate_id -> {outcome, resolution_at}  — first event wins
        resolutions: dict[str, dict] = {}
        try:
            for _mid, data in await r.xrange("stream:system", count=1000):
                try:
                    env = Envelope.from_json(data["data"])
                    action = env.payload.get("action", "")
                    gid = env.payload.get("gate_id", "")
                    if gid and action in _ACTION_TO_OUTCOME and gid not in resolutions:
                        resolutions[gid] = {
                            "outcome": _ACTION_TO_OUTCOME[action],
                            "resolution_at": env.payload.get("resolution_at", env.timestamp),
                        }
                except Exception:
                    continue
        except Exception:
            pass

        gates: list[dict] = []
        try:
            for _mid, data in await r.xrange("stream:human-gates", count=1000):
                try:
                    env = Envelope.from_json(data["data"])
                    res = resolutions.get(env.id)
                    gates.append({
                        "gate_id": env.id,
                        "sender": env.sender_id,
                        "role": env.sender_role,
                        "thread": env.thread_id[:8],
                        "action": env.payload.get("action", "?"),
                        "reason": env.payload.get("reason", ""),
                        "context": env.payload.get("context", ""),
                        "timestamp": env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp,
                        "pending": res is None,
                        "outcome": res["outcome"] if res else "pending",
                        "resolution_at": res["resolution_at"] if res else None,
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return gates

    async def _build_thread_model(self) -> dict:
        """Build enriched thread model with product-facing derived fields."""

        now = time.time()
        r = self.bus.redis

        all_events: list[Envelope] = []
        parse_failures = 0
        for stream_name in ALL_CHANNELS:
            try:
                messages = await r.xrevrange(f"stream:{stream_name}", count=self._stream_read_limit)
                for _mid, data in messages:
                    try:
                        env = Envelope.from_json(data["data"])
                        if env.message_type != MessageType.CLI_TRACE:
                            all_events.append(env)
                    except Exception:
                        parse_failures += 1
                        continue
            except Exception:
                continue

        all_events.sort(key=lambda e: e.timestamp)

        if parse_failures:
            logger.warning("Thread replay: %d envelope parse failures (possible stream corruption)", parse_failures)

        # PR status lookup
        pr_status = {}
        try:
            raw = await r.hgetall(self.PR_SET_KEY)
            for tid, val in raw.items():
                parts = val.split("|", 2)
                s = parts[0]
                if s.startswith("http"):
                    pr_status[tid] = {"status": "created", "url": s}
                elif "failed" in s:
                    pr_status[tid] = {"status": "failed", "detail": parts[2] if len(parts) > 2 else ""}
                elif s == "pending":
                    pr_status[tid] = {"status": "pending"}
                else:
                    pr_status[tid] = {"status": s}
        except Exception:
            pass

        # Thread cycle counts
        cycle_counts = {}
        try:
            raw = await r.hgetall("orchestrator:thread_cycles")
            for k, v in raw.items():
                tid = k.replace(":cycles", "")
                cycle_counts[tid] = int(v)
        except Exception:
            pass

        threads: dict[str, dict] = {}
        for env in all_events:
            mt = env.message_type
            # System events are infrastructure signals, not workflow items.
            # Don't let them create or pollute thread entries.
            if mt == MessageType.SYSTEM:
                continue

            tid = env.thread_id
            if tid not in threads:
                threads[tid] = {
                    "thread_id": tid, "label": "", "branch": "", "events": 0,
                    "stage": "analyzing", "status": "active",
                    "last_type": "", "last_time": "", "last_timestamp": "", "last_decision": "",
                    "blocking_issues": [], "concerns": [], "pr": None, "review_cycles": 0,
                    "proposal_submissions": 0, "proposal_approvals": 0,
                    "proposal_rejections": 0, "proposal_revisions": 0,
                    "task_assignments": 0, "review_approvals": 0,
                }
            t = threads[tid]
            t["events"] += 1
            t["last_type"] = env.message_type.value
            t["last_timestamp"] = env.timestamp
            t["last_time"] = env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp

            p = env.payload

            if mt == MessageType.PROPOSAL:
                t["proposal_submissions"] += 1
                t["label"] = p.get("title", "")[:60]
                t["stage"] = "proposed"
            elif mt == MessageType.PROPOSAL_REVIEW:
                d = p.get("decision", "")
                t["last_decision"] = d
                t["concerns"] = [str(c) for c in p.get("concerns", [])[:5]]
                if d == "approved":
                    t["proposal_approvals"] += 1
                    t["stage"] = "approved"
                    t["status"] = "active"
                elif d == "rejected":
                    t["proposal_rejections"] += 1
                    t["stage"] = "rejected"
                    t["status"] = "failed"
                elif d in ("needs_revision", "needs_clarification"):
                    t["proposal_revisions"] += 1
                    t["stage"] = "revision_requested"
                    t["status"] = "rework"
            elif mt == MessageType.TASK_ASSIGNMENT:
                t["task_assignments"] += 1
                t["branch"] = p.get("branch_name", "")
                t["stage"] = "implementing"
                t["status"] = "active"
                if not t["label"]:
                    t["label"] = t["branch"]
            elif mt == MessageType.TASK_PROGRESS:
                s = p.get("status", "")
                if s == "completed":
                    t["stage"] = "awaiting_review"
                elif s == "blocked":
                    t["status"] = "blocked"
            elif mt == MessageType.REVIEW_REQUEST:
                t["stage"] = "in_review"
            elif mt == MessageType.REVIEW_RESULT:
                d = p.get("decision", "")
                t["last_decision"] = d
                if d == "approved":
                    t["review_approvals"] += 1
                    t["stage"] = "completed"
                    t["status"] = "completed"
                elif d == "changes_requested":
                    t["stage"] = "rework"
                    t["status"] = "rework"
                    t["blocking_issues"] = p.get("blocking_issues", [])[:5]

        # Deliberation/challenger data from cli-traces
        challenger_by_thread: dict[str, dict] = {}
        try:
            traces = await r.xrevrange("stream:cli-traces", count=300)
            for _mid, data in traces:
                try:
                    env = Envelope.from_json(data["data"])
                    p = env.payload
                    summary = p.get("deliberation_summary")
                    if summary and env.thread_id not in challenger_by_thread:
                        challenger_by_thread[env.thread_id] = summary
                except Exception:
                    continue
        except Exception:
            pass

        # Enrich with PR status, cycle counts, and derived semantics
        for tid, t in threads.items():
            t["pr"] = pr_status.get(tid)
            t["review_cycles"] = cycle_counts.get(tid, 0)

            if t["review_cycles"] >= 999:
                # Explicitly abandoned by operator — terminal state
                t["status"] = "failed"
                t["stage"] = "abandoned"
            elif t["review_cycles"] >= self._max_change_rounds and t["status"] != "completed":
                t["status"] = "blocked"
                t["stage"] = "cycle_exhausted"

            # Challenger summary
            cs = challenger_by_thread.get(tid)
            t["challenged"] = cs.get("challenged", False) if cs else False
            t["challenger_summary"] = cs if cs else None

            # Derived semantic fields
            t["goal"] = t["label"] or "Improvement proposal"
            t["has_multiple_proposals"] = t["proposal_submissions"] > 1

            summary_bits = [f"{t['proposal_submissions']} PM submission" + ("" if t["proposal_submissions"] == 1 else "s")]
            if t["proposal_approvals"]:
                summary_bits.append(f"{t['proposal_approvals']} architect-approved")
            if t["proposal_rejections"]:
                summary_bits.append(f"{t['proposal_rejections']} rejected")
            if t["proposal_revisions"]:
                summary_bits.append(f"{t['proposal_revisions']} sent back")
            t["proposal_summary"] = " · ".join(summary_bits)

            if t["has_multiple_proposals"]:
                t["thread_scope"] = (
                    "This thread contains multiple PM proposal submissions or revisions. "
                    "The timeline is a revision history, not a checklist where every historical proposal must finish separately."
                )
            else:
                t["thread_scope"] = "This thread currently represents one proposal path."

            if t["pr"] and t["pr"].get("status", "").startswith("http"):
                t["pr_scope"] = "This thread already has a PR."
            elif t["stage"] == "completed":
                t["pr_scope"] = "This thread is reviewer-approved and can produce one PR."
            else:
                t["pr_scope"] = (
                    "PR creation is per thread, not per historical PM proposal in the timeline. "
                    "Only the active implementation path on this thread can create one PR."
                )

            # Why — the most important thing to communicate
            stage, status = t["stage"], t["status"]
            bi = t["blocking_issues"]
            if status == "blocked" and t["review_cycles"] >= self._max_change_rounds:
                t["why"] = f"Blocked after {t['review_cycles']} review cycles"
                t["blocked_reason"] = bi[0] if bi else "Review cycle limit reached"
                t["needs_human"] = True
            elif status == "rework" and bi:
                t["why"] = "Changes requested: " + bi[0][:200]
                t["blocked_reason"] = bi[0] if bi else ""
                t["needs_human"] = False
            elif status == "rework" and t.get("concerns"):
                t["why"] = "Needs revision: " + t["concerns"][0][:200]
                t["blocked_reason"] = t["concerns"][0] if t["concerns"] else ""
                t["needs_human"] = False
            elif status == "failed":
                t["why"] = "Rejected by architect"
                t["blocked_reason"] = ""
                t["needs_human"] = False
            elif t["pr"] and t["pr"].get("status") == "failed":
                t["why"] = "PR creation failed"
                t["blocked_reason"] = t["pr"].get("detail", "")
                t["needs_human"] = True
            else:
                t["why"] = ""
                t["blocked_reason"] = ""
                t["needs_human"] = False

            # Current state — human readable
            state_map = {
                "analyzing": "Being analyzed by PM",
                "proposed": "Proposal submitted, awaiting architect",
                "approved": "Architect approved, awaiting developer",
                "revision_requested": "PM revising based on architect feedback",
                "implementing": "Developer implementing on " + (t["branch"] or "agent branch"),
                "awaiting_review": "Implementation complete, awaiting reviewer",
                "in_review": "Under code review",
                "rework": "Developer reworking after reviewer feedback",
                "completed": "Approved and done",
                "rejected": "Rejected by architect",
                "cycle_exhausted": f"Blocked after {t['review_cycles']} review cycles",
                "abandoned": "Abandoned by operator",
            }
            t["current_state"] = state_map.get(stage, stage)

            # Next step
            next_map = {
                "analyzing": "PM will produce structured proposals",
                "proposed": "Architect will review for feasibility and risk",
                "approved": "Developer will be assigned to implement",
                "revision_requested": "PM will revise and resubmit",
                "implementing": "Developer will submit for code review when done",
                "awaiting_review": "Reviewer will evaluate the implementation",
                "in_review": "Reviewer will approve or request changes",
                "rework": "Developer will address reviewer feedback and resubmit",
                "completed": "PR created or ready for merge",
                "rejected": "No further action unless resubmitted",
                "abandoned": "No further action",
                "cycle_exhausted": "Use Reset to retry or Abandon to skip permanently",
            }
            t["next_step"] = next_map.get(stage, "")

            # Latest change — transition phrasing instead of steady-state labels
            latest_change = None
            if t["last_type"] == "review_result":
                if t["last_decision"] == "changes_requested":
                    latest_change = {
                        "kind": "rework",
                        "summary": "Reviewer requested changes",
                        "detail": "This thread moved back to rework.",
                        "time": t["last_time"],
                        "timestamp": t["last_timestamp"],
                    }
                elif t["last_decision"] == "approved":
                    latest_change = {
                        "kind": "approved",
                        "summary": "Reviewer approved the implementation",
                        "detail": "This thread is now ready for PR creation or merge.",
                        "time": t["last_time"],
                        "timestamp": t["last_timestamp"],
                    }
            elif t["last_type"] == "proposal_review":
                if t["last_decision"] == "approved":
                    latest_change = {
                        "kind": "approved",
                        "summary": "Architect approved the proposal",
                        "detail": "This thread moved into implementation.",
                        "time": t["last_time"],
                        "timestamp": t["last_timestamp"],
                    }
                elif t["last_decision"] in ("needs_revision", "needs_clarification"):
                    latest_change = {
                        "kind": "sent_back",
                        "summary": "Architect sent this back to PM",
                        "detail": "PM needs to revise and resubmit the proposal.",
                        "time": t["last_time"],
                        "timestamp": t["last_timestamp"],
                    }
                elif t["last_decision"] == "rejected":
                    latest_change = {
                        "kind": "rejected",
                        "summary": "Architect rejected the proposal",
                        "detail": "This thread will not continue unless resubmitted.",
                        "time": t["last_time"],
                        "timestamp": t["last_timestamp"],
                    }
            elif t["last_type"] == "proposal":
                latest_change = {
                    "kind": "proposal",
                    "summary": "PM submitted a proposal",
                    "detail": "Architect review is next.",
                    "time": t["last_time"],
                    "timestamp": t["last_timestamp"],
                }
            elif t["last_type"] == "task_assignment":
                latest_change = {
                    "kind": "assigned",
                    "summary": "Developer was assigned",
                    "detail": "Implementation is now underway.",
                    "time": t["last_time"],
                    "timestamp": t["last_timestamp"],
                }
            elif t["last_type"] == "review_request":
                latest_change = {
                    "kind": "in_review",
                    "summary": "Work entered review",
                    "detail": "Reviewer is evaluating the implementation.",
                    "time": t["last_time"],
                    "timestamp": t["last_timestamp"],
                }
            elif t["stage"] == "cycle_exhausted":
                latest_change = {
                    "kind": "blocked",
                    "summary": "Thread was blocked after repeated review cycles",
                    "detail": "Operator action is needed to retry or abandon it.",
                    "time": t["last_time"],
                    "timestamp": t["last_timestamp"],
                }
            elif t["stage"] == "abandoned":
                latest_change = {
                    "kind": "abandoned",
                    "summary": "Thread was abandoned",
                    "detail": "It is no longer part of active workflow.",
                    "time": t["last_time"],
                    "timestamp": t["last_timestamp"],
                }
            t["latest_change"] = latest_change

        return threads

    async def _handle_thread_detail(self, request: web.Request) -> web.Response:
        thread_id = request.match_info["thread_id"]
        r = self.bus.redis
        events = []
        trace_events = []
        for stream_name in ALL_CHANNELS:
            try:
                messages = await r.xrevrange(f"stream:{stream_name}", count=self._stream_read_limit)
                messages.reverse()
                for _mid, data in messages:
                    try:
                        env = Envelope.from_json(data["data"])
                        if env.thread_id == thread_id and env.message_type != MessageType.CLI_TRACE:
                            events.append({"type": env.message_type.value, "sender": env.sender_id, "role": env.sender_role, "timestamp": env.timestamp, "payload": env.payload})
                    except Exception:
                        continue
            except Exception:
                continue
        events.sort(key=lambda e: e["timestamp"])

        try:
            traces = await r.xrevrange("stream:cli-traces", count=min(self._stream_read_limit * 2, 1000))
            traces.reverse()
            for _mid, data in traces:
                try:
                    env = Envelope.from_json(data["data"])
                    if env.thread_id != thread_id or env.message_type != MessageType.CLI_TRACE:
                        continue
                    p = env.payload
                    trace_events.append({
                        "agent": env.sender_id,
                        "role": env.sender_role,
                        "timestamp": env.timestamp,
                        "direction": p.get("direction", ""),
                        "content": p.get("content", ""),
                        "content_preview": p.get("content_preview", ""),
                        "content_length": p.get("content_length", 0),
                        "duration_ms": p.get("duration_ms", 0),
                        "model": p.get("model", ""),
                        "cli_model": p.get("cli_model", ""),
                        "cli_backend": p.get("cli_backend", ""),
                        "input_tokens": p.get("input_tokens", 0),
                        "output_tokens": p.get("output_tokens", 0),
                        "cost_usd": p.get("cost_usd", 0),
                        "deliberation_round": p.get("deliberation_round"),
                        "is_error": p.get("is_error", False),
                    })
                except Exception:
                    continue
        except Exception:
            pass

        def pick_display_response(responses: list[dict]) -> dict | None:
            if not responses:
                return None
            repairs = [r for r in responses if r.get("model") == "repair"]
            if repairs:
                return repairs[-1]
            primaries = [r for r in responses if r.get("model") == "primary"]
            if primaries:
                return primaries[-1]
            non_challenger = [r for r in responses if r.get("model") != "challenger"]
            if non_challenger:
                return non_challenger[-1]
            return responses[-1]

        transcript_turns = []
        current_turn: dict | None = None
        for trace in trace_events:
            if trace["direction"] == "prompt":
                if current_turn is not None:
                    display_response = pick_display_response(current_turn["responses"])
                    hidden_count = max(0, len(current_turn["responses"]) - (1 if display_response else 0))
                    transcript_turns.append({
                        "agent": current_turn["agent"],
                        "role": current_turn["role"],
                        "timestamp": current_turn["timestamp"],
                        "prompt": current_turn["prompt"],
                        "response": display_response,
                        "hidden_trace_count": hidden_count,
                    })
                current_turn = {
                    "agent": trace["agent"],
                    "role": trace["role"],
                    "timestamp": trace["timestamp"],
                    "prompt": trace,
                    "responses": [],
                }
            elif current_turn is not None:
                current_turn["responses"].append(trace)

        if current_turn is not None:
            display_response = pick_display_response(current_turn["responses"])
            hidden_count = max(0, len(current_turn["responses"]) - (1 if display_response else 0))
            transcript_turns.append({
                "agent": current_turn["agent"],
                "role": current_turn["role"],
                "timestamp": current_turn["timestamp"],
                "prompt": current_turn["prompt"],
                "response": display_response,
                "hidden_trace_count": hidden_count,
            })

        # Separate thread-level system events from proposal-scoped events.
        # Thread-level actions (cycles_reset, thread_abandoned, cli_timeout)
        # apply to the whole thread, not to a specific proposal.
        _THREAD_LEVEL_ACTIONS = {"cycles_reset", "thread_abandoned", "cli_timeout", "pr_created", "pr_merged", "pr_closed", "pr_failed", "pr_skipped"}

        def _is_thread_level(event: dict) -> bool:
            if event["type"] != "system":
                return False
            return event["payload"].get("action", "") in _THREAD_LEVEL_ACTIONS

        groups = []
        thread_events = []  # events that apply to the whole thread
        current = None

        def ensure_group() -> dict:
            nonlocal current
            if current is None:
                current = {
                    "proposal_index": 1,
                    "title": "Thread activity",
                    "timestamp": events[0]["timestamp"] if events else "",
                    "stage": "analyzing",
                    "status": "active",
                    "last_decision": "",
                    "is_current": True,
                    "events": [],
                }
                groups.append(current)
            return current

        def apply_event_state(group: dict, event: dict) -> None:
            et = event["type"]
            p = event["payload"]
            if et == "proposal":
                group["title"] = p.get("title", "")[:80] or f"Proposal {group['proposal_index']}"
                group["stage"] = "proposed"
                group["status"] = "active"
            elif et == "proposal_review":
                d = p.get("decision", "")
                group["last_decision"] = d
                if d == "approved":
                    group["stage"] = "approved"
                    group["status"] = "active"
                elif d == "rejected":
                    group["stage"] = "rejected"
                    group["status"] = "failed"
                elif d in ("needs_revision", "needs_clarification"):
                    group["stage"] = "revision_requested"
                    group["status"] = "rework"
            elif et == "task_assignment":
                group["stage"] = "implementing"
                group["status"] = "active"
            elif et == "task_progress":
                s = p.get("status", "")
                if s == "completed":
                    group["stage"] = "awaiting_review"
                elif s == "blocked":
                    group["status"] = "blocked"
            elif et == "review_request":
                group["stage"] = "in_review"
            elif et == "review_result":
                d = p.get("decision", "")
                group["last_decision"] = d
                if d == "approved":
                    group["stage"] = "completed"
                    group["status"] = "completed"
                elif d == "changes_requested":
                    group["stage"] = "rework"
                    group["status"] = "rework"

        for event in events:
            # Thread-level events go to a separate list
            if _is_thread_level(event):
                thread_events.append(event)
                continue

            if event["type"] == "proposal":
                # Mark previous group as no longer current
                if current is not None:
                    current["is_current"] = False
                current = {
                    "proposal_index": len(groups) + 1,
                    "title": event["payload"].get("title", "")[:80] or f"Proposal {len(groups) + 1}",
                    "timestamp": event["timestamp"],
                    "stage": "proposed",
                    "status": "active",
                    "last_decision": "",
                    "is_current": True,
                    "events": [],
                }
                groups.append(current)
            group = ensure_group()
            group["events"].append(event)
            apply_event_state(group, event)

        return web.json_response({
            "thread_id": thread_id,
            "proposal_groups": groups,
            "thread_events": thread_events,
            "events": events,
            "transcript_turns": transcript_turns,
            "raw_traces": trace_events,
        })

    async def _handle_agent_detail(self, request: web.Request) -> web.Response:
        agent_id = request.match_info["agent_id"]
        r = self.bus.redis
        events = []
        for stream_name in ALL_CHANNELS:
            try:
                messages = await r.xrevrange(f"stream:{stream_name}", count=200)
                for _mid, data in messages:
                    try:
                        env = Envelope.from_json(data["data"])
                        if env.sender_id == agent_id and env.message_type != MessageType.CLI_TRACE:
                            events.append({"type": env.message_type.value, "thread": env.thread_id, "thread_short": env.thread_id[:8], "timestamp": env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp, "payload": env.payload})
                    except Exception:
                        continue
            except Exception:
                continue
        events.sort(key=lambda e: e["timestamp"])
        return web.json_response(events[-20:])

    async def _handle_stream_messages(self, request: web.Request) -> web.Response:
        stream_name = request.match_info["stream_name"]
        r = self.bus.redis
        out = []
        try:
            raw = await r.xrevrange(f"stream:{stream_name}", count=30)
            for _mid, data in raw:
                try:
                    env = Envelope.from_json(data["data"])
                    if env.message_type == MessageType.CLI_TRACE:
                        continue
                    out.append({"type": env.message_type.value, "sender": env.sender_id, "role": env.sender_role, "thread": env.thread_id[:8], "timestamp": env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp, "payload": env.payload})
                except Exception:
                    continue
        except Exception:
            pass
        out.reverse()
        return web.json_response(out)

    async def _handle_gates(self, request: web.Request) -> web.Response:
        return web.json_response(await self._get_gates_data())

    def _check_gate_auth(self, request: web.Request) -> web.Response | None:
        """Verify Bearer token on gate mutation requests. Returns error response or None."""
        if not self.gate_token:
            return None
        auth = request.headers.get("Authorization", "")
        if not auth:
            return web.json_response(
                {"error": "missing authorization"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        parts = auth.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer" or parts[1] != self.gate_token:
            return web.json_response(
                {"error": "invalid token"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    async def _handle_gate_approve(self, request: web.Request) -> web.Response:
        if err := self._check_gate_auth(request):
            return err
        return await self._respond_gate(request, "approval_granted")

    async def _handle_gate_deny(self, request: web.Request) -> web.Response:
        if err := self._check_gate_auth(request):
            return err
        return await self._respond_gate(request, "approval_denied")

    async def _respond_gate(self, request: web.Request, action: str) -> web.Response:
        gate_id = request.match_info["gate_id"]
        r = self.bus.redis
        thread_id = ""
        found = False
        try:
            for _mid, data in await r.xrange("stream:human-gates"):
                env = Envelope.from_json(data["data"])
                if env.id == gate_id:
                    thread_id = env.thread_id
                    found = True
                    break
        except Exception:
            pass

        if not found:
            return web.json_response({"error": "gate not found"}, status=404)

        response_env = Envelope(sender_id="human", sender_role="human", message_type=MessageType.SYSTEM, payload={"action": action, "gate_id": gate_id}, thread_id=thread_id)
        await self.bus.publish(f"gate-responses:{gate_id}", response_env)
        await self.bus.publish("system", response_env)
        return web.json_response({"status": "ok", "action": action, "gate_id": gate_id})

    async def _handle_agent_pause(self, request: web.Request) -> web.Response:
        agent_id = request.match_info["agent_id"]
        await self.bus.redis.set(f"agent:{agent_id}:paused", "1")
        return web.json_response({"status": "paused", "agent_id": agent_id})

    async def _handle_agent_resume(self, request: web.Request) -> web.Response:
        agent_id = request.match_info["agent_id"]
        await self.bus.redis.delete(f"agent:{agent_id}:paused")
        return web.json_response({"status": "resumed", "agent_id": agent_id})

    async def _handle_thread_reset_cycles(self, request: web.Request) -> web.Response:
        """Reset review cycle count for a blocked thread so it can retry."""
        if err := self._check_gate_auth(request):
            return err
        thread_id = request.match_info["thread_id"]
        r = self.bus.redis
        try:
            await r.hdel(THREAD_CYCLES_KEY, f"{thread_id}:cycles")
            requeued_stage = None
            for stage, input_stream in (
                ("reviews", "review-requests"),
                ("tasks", "tasks"),
                ("proposals", "proposals"),
            ):
                _set_key, msg_key, ts_key = _active_keys(stage)
                raw_env = await r.hget(msg_key, thread_id)
                if not raw_env:
                    continue
                await r.xadd(f"stream:{input_stream}", {"data": raw_env})
                await r.hset(ts_key, thread_id, str(time.time()))
                await r.delete(f"orchestrator:stall_surfaced:{stage}:{thread_id}")
                requeued_stage = stage
                break
            await self.bus.publish("system", Envelope(
                sender_id="web",
                sender_role="system",
                message_type=MessageType.SYSTEM,
                payload={
                    "action": "cycles_reset",
                    "thread_id": thread_id,
                    "requeued_stage": requeued_stage,
                },
                thread_id=thread_id,
            ))
            logger.info("Reset review cycles for thread %s", thread_id[:8])
            return web.json_response({
                "status": "reset",
                "thread_id": thread_id,
                "requeued_stage": requeued_stage,
            })
        except Exception as e:
            return web.json_response({"error": str(e)[:200]}, status=500)

    async def _handle_thread_abandon(self, request: web.Request) -> web.Response:
        """Mark a thread as abandoned so it stops showing as an exception."""
        if err := self._check_gate_auth(request):
            return err
        thread_id = request.match_info["thread_id"]
        r = self.bus.redis
        try:
            # Set a high cycle count to permanently block, and publish event
            await r.hset(THREAD_CYCLES_KEY, f"{thread_id}:cycles", "999")
            await _clear_active_work_thread(r, thread_id)
            # Clean up the agent branch
            branch = await r.hget("orchestrator:thread_branches", thread_id)
            if branch:
                import subprocess
                subprocess.run(["git", "branch", "-D", branch], capture_output=True)
                await r.hdel("orchestrator:thread_branches", thread_id)
                logger.info("Deleted branch %s for abandoned thread %s", branch, thread_id[:8])
            await self.bus.publish("system", Envelope(
                sender_id="web",
                sender_role="system",
                message_type=MessageType.SYSTEM,
                payload={"action": "thread_abandoned", "thread_id": thread_id},
                thread_id=thread_id,
            ))
            logger.info("Abandoned thread %s", thread_id[:8])
            return web.json_response({"status": "abandoned", "thread_id": thread_id})
        except Exception as e:
            return web.json_response({"error": str(e)[:200]}, status=500)

    async def _handle_traces(self, request: web.Request) -> web.Response:
        """Return recent CLI traces for the timeline visualization."""
        r = self.bus.redis
        limit = min(int(request.query.get("limit", "200")), 1000)
        traces = await r.xrevrange("stream:cli-traces", count=limit)
        result = []
        for _mid, data in reversed(traces):
            try:
                env = Envelope.from_json(data["data"])
                p = env.payload
                if p.get("direction") != "response":
                    continue
                result.append({
                    "agent": env.sender_id,
                    "role": env.sender_role,
                    "thread_id": env.thread_id,
                    "timestamp": env.timestamp,
                    "duration_ms": p.get("duration_ms", 0),
                    "model": p.get("cli_model") or p.get("model", ""),
                    "cli_backend": p.get("cli_backend", ""),
                    "input_tokens": p.get("input_tokens", 0),
                    "output_tokens": p.get("output_tokens", 0),
                    "cost_usd": p.get("cost_usd", 0),
                    "deliberation_round": p.get("deliberation_round"),
                    "delib_model": p.get("model", ""),
                    "is_error": p.get("is_error", False),
                })
            except Exception:
                continue
        return web.json_response(result)

    async def _handle_audit(self, request: web.Request) -> web.Response:
        """Run pipeline health audit and return findings."""
        findings = await audit_pipeline(self.redis_url)
        return web.json_response([f.to_dict() for f in findings])
