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
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from agents.core.auditor import audit_pipeline
from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus
from agents.core.mode import MODES, get_mode, set_mode
from agents.core import refusals
from agents.core.redis_keys import _active_keys, _clear_active_work_thread
from agents.core.state import ROLE_TO_PHASE, derive_current_phase, load_snapshot
from agents.core.supervisor import (
    SupervisorError,
    clear_start_failure,
    is_running,
    last_start_failure,
    spawn_orchestrator,
)
from agents.core.thread_guard import THREAD_CYCLES_KEY

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

ALL_CHANNELS = [
    "proposals", "design-feedback", "reviews", "tasks", "review-requests",
    "review-results", "progress", "user-gates", "system",
]


class WebDashboard:
    _ACTIVE_WORK_STAGES = ("designs", "proposals", "tasks", "reviews")

    def __init__(self, redis_url: str, port: int = 8081, gate_token: str | None = None,
                 idle_threshold: int = 600, max_change_rounds: int = 3, stream_read_limit: int = 500,
                 max_pending_proposals: int = 3, max_pending_tasks: int = 3, max_pending_reviews: int = 5,
                 weekly_token_budget: int = 0, weekly_token_basis: str = "both",
                 safety=None, gate_timeout: int = 0, agent_roles=None):
        self.redis_url = redis_url
        self.port = port
        self.gate_token = gate_token
        self._idle_threshold = idle_threshold
        self._max_change_rounds = max_change_rounds
        self._stream_read_limit = stream_read_limit
        self._wip_limits = {
            "designs": max_pending_proposals,
            "proposals": max_pending_proposals,
            "tasks": max_pending_tasks,
            "reviews": max_pending_reviews,
        }
        self.safety = safety
        self.agent_roles = agent_roles or []
        self._gate_timeout_hours = gate_timeout / 3600 if gate_timeout else 0
        self.weekly_token_budget = weekly_token_budget
        self.weekly_token_basis = weekly_token_basis
        self.bus = MessageBus(redis_url)

    async def start(self) -> None:
        await self.bus.connect()

        app = web.Application(middlewares=[self._private_network_middleware])

        # API routes
        app.router.add_get("/api/snapshot", self._handle_snapshot)
        app.router.add_get("/api/events", self._handle_events_sse)
        app.router.add_get("/api/threads", self._handle_threads)
        app.router.add_get("/api/threads/{thread_id}", self._handle_thread_detail)
        app.router.add_get("/api/agents/{agent_id}", self._handle_agent_detail)
        app.router.add_get("/api/streams/{stream_name}", self._handle_stream_messages)
        app.router.add_get("/api/policy", self._handle_policy)
        app.router.add_get("/api/usage", self._handle_usage)
        app.router.add_get("/api/errors", self._handle_errors)
        app.router.add_get("/api/refused", self._handle_refused_list)
        app.router.add_post("/api/refused/{refusal_id}/reinstate", self._handle_refused_reinstate)
        app.router.add_post("/api/refused/{refusal_id}/restart", self._handle_refused_restart)
        app.router.add_get("/api/mode", self._handle_mode_get)
        app.router.add_post("/api/mode", self._handle_mode_set)
        app.router.add_post("/api/proposals", self._handle_submit_proposal)
        app.router.add_get("/api/orchestrator", self._handle_orchestrator_status)
        app.router.add_post("/api/orchestrator/start", self._handle_orchestrator_start)
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
            return web.json_response({"error": str(e)}, status=500)

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
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the page with its assets versioned by their own modification time.

        WHY THIS EXISTS. index.html references /static/style.css and /static/app.js with
        no version, so a browser holding them cached keeps using them and an edit to
        either simply does not arrive. That cost real time more than once: a CSS rule was
        written correctly, served correctly, fetched correctly, and still had no effect on
        the open page — which reads exactly like a rule that does not work.

        The mtime changes when the file changes and never otherwise, so the cache keeps
        working; it just cannot serve a stale copy of something that has been edited.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("style.css", "app.js"):
            try:
                version = int((STATIC_DIR / asset).stat().st_mtime)
            except OSError:
                continue
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
        # THE PAGE ITSELF MUST NOT BE CACHED. It is the only thing that knows which
        # version of each asset to ask for, so a cached copy pins the browser to whatever
        # they were when it was stored. That is exactly what happened while building this:
        # the versioning was correct and served correctly and still had no effect, because
        # the page carrying it came from cache. The assets can be cached hard now — they
        # are versioned.
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    # ── API handlers ───────────────────────────────────────

    async def _handle_snapshot(self, request: web.Request) -> web.Response:
        try:
            snapshot = await load_snapshot(self.redis_url)
        except Exception:
            return web.json_response({"error": "Redis unreachable"}, status=503)
        data = asdict(snapshot)

        # Why the last start died, if it did. Carried on the snapshot so a phone that
        # reloads - or a second tab that never clicked the button - still sees the reason
        # instead of a bare "disconnected". Added here rather than on the shared Snapshot
        # dataclass, which the terminal monitor and the CLI also read and neither needs.
        data["orchestrator_start_error"] = last_start_failure() or ""

        # Weekly token usage against the plan allowance. The dashboard renders a ratio
        # rather than an amount, because on a subscription "tokens used / tokens
        # available" is the reference point and a dollar figure is not.
        #
        # The week is computed HERE rather than in the browser: a phone in another
        # timezone would otherwise read a different ISO week than the one the agents
        # wrote to, and silently show zero.
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        wk = f"{year}-W{week:02d}"
        m = data.get("metrics", {}) or {}
        used_in = int(m.get(f"tokens_in:week:{wk}", 0) or 0)
        used_out = int(m.get(f"tokens_out:week:{wk}", 0) or 0)
        basis = (self.weekly_token_basis or "both").lower()
        used = used_out if basis == "output" else used_in if basis == "input" else used_in + used_out
        data["weekly"] = {
            "week": wk,
            "tokens_in": used_in,
            "tokens_out": used_out,
            "used": used,
            "budget": self.weekly_token_budget,
            "basis": basis,
            "cost_mc": int(m.get(f"cost_mc:week:{wk}", 0) or 0),
            "pct": round(used * 100.0 / self.weekly_token_budget, 1) if self.weekly_token_budget else None,
        }

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
                            # No local import: `datetime` is imported at module level,
                            # and re-importing it here made it a LOCAL name for the whole
                            # function - so any use of it earlier in the same function
                            # raised UnboundLocalError. Python binds by function scope,
                            # not by line order.
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
        phase = derive_current_phase(snapshot.agents, bp)

        # WHICH ROLE IS WAITING ON YOU. A role holding an unanswered gate is not idle and
        # it is not working — it is stopped, on purpose, pending a decision that only the
        # operator can make. That is a different state from "active" and the header draws
        # it differently: bold for working, underlined for waiting on you.
        try:
            waiting = {
                ROLE_TO_PHASE.get(g.get("role"), g.get("role"))
                for g in await self._get_gates_data()
                if g.get("pending")
            }
        except Exception:
            waiting = set()
        for entry in phase.get("phases", []):
            entry["awaiting"] = entry["name"] in waiting

        data["pipeline_phase"] = phase

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
            for _mid, data in await r.xrange("stream:user-gates", count=1000):
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
            t["last_sender_role"] = env.sender_role
            t["last_timestamp"] = env.timestamp
            t["last_time"] = env.timestamp[11:19] if len(env.timestamp) > 19 else env.timestamp

            p = env.payload

            if mt == MessageType.PROPOSAL:
                t["proposal_submissions"] += 1
                t["label"] = p.get("title", "")
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
                t["needs_user"] = True
            # Not cut to 200 here. The API truncating is what made the detail view
            # unreadable; a list shortens in CSS, where it knows how much room it has.
            elif status == "rework" and bi:
                t["why"] = "Changes requested: " + bi[0]
                t["blocked_reason"] = bi[0] if bi else ""
                t["needs_user"] = False
            elif status == "rework" and t.get("concerns"):
                t["why"] = "Needs revision: " + t["concerns"][0]
                t["blocked_reason"] = t["concerns"][0] if t["concerns"] else ""
                t["needs_user"] = False
            elif status == "failed":
                t["why"] = "Rejected by architect"
                t["blocked_reason"] = ""
                t["needs_user"] = False
            elif t["pr"] and t["pr"].get("status") == "failed":
                t["why"] = "PR creation failed"
                t["blocked_reason"] = t["pr"].get("detail", "")
                t["needs_user"] = True
            else:
                t["why"] = ""
                t["blocked_reason"] = ""
                t["needs_user"] = False

            # Current state — user readable
            state_map = {
                "analyzing": "Being analyzed by PM",
                "proposed": "Proposal submitted, awaiting technical review",
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
                    # Saying "PM" for a manually submitted proposal is not a cosmetic
                    # slip: it credits an agent for a decision made by hand, and makes it
                    # impossible to tell from the feed which proposals were asked for and
                    # which were volunteered.
                    "summary": (
                        "Manually submitted proposal"
                        if t.get("last_sender_role") == "user"
                        else "PM submitted a proposal"
                    ),
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
                group["title"] = p.get("title", "") or f"Proposal {group['proposal_index']}"
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
                    "title": event["payload"].get("title", "") or f"Proposal {len(groups) + 1}",
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

    @web.middleware
    async def _private_network_middleware(self, request: web.Request, handler):
        """Answer CORS preflights, including Chrome's Private Network Access check.

        WHY THIS IS NEEDED FOR A SAME-ORIGIN REQUEST. When the dashboard is reached over
        plain HTTP at a private address - a phone on a Tailscale or LAN address - Chrome
        treats the insecure page as "public" address space and the request to a 100.x or
        192.168.x host as a private-network request. It then sends an OPTIONS preflight
        carrying `Access-Control-Request-Private-Network: true`, even though the page and
        the API share an origin.

        aiohttp has no OPTIONS route, so that preflight was answered 405, the browser
        discarded the real request, and `fetch` rejected with a bare TypeError. From a
        phone the button simply reported "failed: type error" with nothing else to go on,
        while every test from localhost passed - loopback is not a private-network
        request, so the preflight never happened there.

        The response is deliberately narrow: it echoes the requesting origin rather than
        using a wildcard, because `Authorization` is a credentialed header and `*` is
        invalid with credentials. This is not opening the dashboard to other sites - it
        is letting the dashboard talk to itself.
        """
        # Request log to a file. A browser on another device cannot show you its console,
        # so without this the only evidence of a failure is the word the UI managed to
        # print. Records method, path, origin and whether an Authorization header was
        # present - never the token itself.
        try:
            with open(Path(__file__).resolve().parent / "logs" / "dashboard-requests.log", "a",
                      encoding="utf-8") as fh:
                fh.write("{} {} {} origin={!r} auth={} ua={!r}\n".format(
                    time.strftime("%H:%M:%S"), request.method, request.path,
                    request.headers.get("Origin", ""),
                    "yes" if request.headers.get("Authorization") else "no",
                    request.headers.get("User-Agent", ""),
                ))
        except OSError:
            pass

        if request.method == "OPTIONS":
            return web.Response(status=204, headers=self._cors_headers(request))
        response = await handler(request)
        for k, v in self._cors_headers(request).items():
            response.headers.setdefault(k, v)
        return response

    @staticmethod
    def _cors_headers(request: web.Request) -> dict[str, str]:
        origin = request.headers.get("Origin", "")
        headers = {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
            # The Private Network Access opt-in. Without it Chrome fails the preflight
            # even when everything else is correct.
            "Access-Control-Allow-Private-Network": "true",
        }
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Vary"] = "Origin"
        return headers





    async def _handle_errors(self, request: web.Request) -> web.Response:
        """Agent failures with their actual messages, newest first.

        The dashboard could previously only report counters — "errors:architect: 17" — which
        names a number and not a problem. Agents publish agent_error events carrying the
        exception type, its message and what they were handling at the time; this reads
        them back so the interface can say what went wrong.
        """
        out = []
        try:
            entries = await self.bus.redis.xrevrange("stream:system", count=self._stream_read_limit)
        except Exception:
            entries = []

        for _mid, fields in entries or []:
            try:
                env = json.loads(fields.get("data") or "{}")
            except (json.JSONDecodeError, AttributeError):
                continue
            payload = env.get("payload") or {}
            if payload.get("action") != "agent_error":
                continue
            ts = env.get("timestamp", "")
            out.append({
                "agent_id": payload.get("agent_id", ""),
                "role": payload.get("role", ""),
                "error_type": payload.get("error_type", "Error"),
                "error": payload.get("error", ""),
                "message_type": payload.get("message_type", ""),
                "thread_id": env.get("thread_id", ""),
                "time": ts[11:19] if len(ts) > 19 else ts,
                "timestamp": ts,
            })

        return web.json_response({"errors": out})

    # ── Usage ──────────────────────────────────────────────

    async def _handle_usage(self, request: web.Request) -> web.Response:
        """Tokens and cost, sliced every way that makes an anomaly visible.

        A single total answers "is this expensive?" and nothing else. An anomaly is always
        a COMPARISON — this agent against the others, this proposal against the last one,
        this hour against the rest of the day — so the slices are the point, not the sum.
        Reported here so that spotting one never requires leaving for external tooling.

        Cost is stored as millicents (cost_mc) to keep the counters integral; it is divided
        back out here so nothing downstream has to know that.
        """
        try:
            m = await self.bus.redis.hgetall("orchestrator:metrics") or {}
        except Exception:
            m = {}

        def num(key: str) -> int:
            try:
                return int(m.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        def collect(prefix: str) -> dict:
            """Every bucket under a prefix, e.g. tokens_in:day: -> {"2026-09-21": 1234}."""
            out = {}
            for k in m:
                if k.startswith(prefix):
                    out[k[len(prefix):]] = num(k)
            return out

        def rows(bucket: str, keys=None) -> list:
            """One row per bucket key, carrying all three figures together."""
            names = keys if keys is not None else sorted(
                set(collect(f"tokens_in:{bucket}:"))
                | set(collect(f"tokens_out:{bucket}:"))
                | set(collect(f"cost_mc:{bucket}:"))
            )
            out = []
            for name in names:
                tin = num(f"tokens_in:{bucket}:{name}")
                tout = num(f"tokens_out:{bucket}:{name}")
                mc = num(f"cost_mc:{bucket}:{name}")
                if not (tin or tout or mc):
                    continue
                out.append({
                    "key": name,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "tokens": tin + tout,
                    "cost_usd": mc / 100_000,
                })
            return out

        # Roles are not stored under a bucket prefix — the role IS the suffix — so they are
        # read from the configured agent names rather than guessed from key shapes.
        role_names = sorted(self.agent_roles) if self.agent_roles else []
        per_role = []
        for r in role_names:
            tin, tout, mc = num(f"tokens_in:{r}"), num(f"tokens_out:{r}"), num(f"cost_mc:{r}")
            if not (tin or tout or mc):
                continue
            per_role.append({
                "key": r, "tokens_in": tin, "tokens_out": tout,
                "tokens": tin + tout, "cost_usd": mc / 100_000,
            })

        per_role.sort(key=lambda r: -r["tokens"])
        threads = sorted(rows("thread"), key=lambda r: -r["tokens"])
        days = sorted(rows("day"), key=lambda r: r["key"], reverse=True)
        hours = sorted(rows("hour"), key=lambda r: r["key"], reverse=True)[:48]
        weeks = sorted(rows("week"), key=lambda r: r["key"], reverse=True)

        return web.json_response({
            "total": {
                "tokens_in": num("tokens_in:total"),
                "tokens_out": num("tokens_out:total"),
                "tokens": num("tokens_in:total") + num("tokens_out:total"),
                "cost_usd": num("cost_mc:total") / 100_000,
            },
            "by_role": per_role,
            "by_thread": threads,
            "by_week": weeks,
            "by_day": days,
            "by_hour": hours,
        })

    # ── Refused work, and the two ways back ────────────────

    async def _handle_refused_list(self, request: web.Request) -> web.Response:
        """Work that was refused at a gate and can still be revisited."""
        items = await refusals.list_all(self.bus.redis)
        return web.json_response({"refused": items})

    async def _handle_refused_reinstate(self, request: web.Request) -> web.Response:
        """Approve refused work after all, unchanged.

        For a refusal made in error, or one whose reason has gone away while the project
        has not moved: the task was right, the moment was wrong. The message is published
        exactly as the agent wrote it, straight to the channel it was bound for — no gate
        this time, because this IS the approval.
        """
        if err := self._check_gate_auth(request):
            return err

        refusal_id = request.match_info["refusal_id"]
        entry = await refusals.get(self.bus.redis, refusal_id)
        if not entry:
            return web.json_response({"error": "no such refusal"}, status=404)

        channel = refusals.CHANNEL_FOR_TYPE.get(entry.get("message_type", ""))
        if not channel:
            return web.json_response(
                {"error": f"cannot reinstate a {entry.get('message_type')} message"},
                status=400,
            )

        env = Envelope(
            sender_id="user",
            sender_role="user",
            message_type=MessageType(entry["message_type"]),
            payload=entry.get("payload", {}),
            thread_id=entry.get("thread_id") or "",
        )
        await self.bus.publish(channel, env)
        await refusals.resolve(self.bus.redis, refusal_id)
        logger.info(
            "Reinstated refusal %s onto %s (thread %s)",
            refusal_id, channel, env.thread_id[:8],
        )
        return web.json_response({"reinstated": True, "thread_id": env.thread_id})

    async def _handle_refused_restart(self, request: web.Request) -> web.Response:
        """Run it again from the proposal that produced it, on a fresh thread.

        For when the PROJECT has moved. The refused task was reasoned out against a
        codebase that no longer exists, so reusing its conclusion would be reusing a stale
        premise — the question has to be asked again rather than answered from a cached
        result. The original proposal is republished unchanged; everything after it is
        derived fresh.
        """
        if err := self._check_gate_auth(request):
            return err

        refusal_id = request.match_info["refusal_id"]
        entry = await refusals.get(self.bus.redis, refusal_id)
        if not entry:
            return web.json_response({"error": "no such refusal"}, status=404)

        thread_id = entry.get("thread_id") or ""
        proposal = await self._find_proposal_for_thread(thread_id)
        if proposal is None:
            return web.json_response(
                {"error": "the proposal behind this work is no longer in the stream; "
                          "submit it again by hand"},
                status=404,
            )

        # A NEW thread: this is a fresh attempt, not a continuation of the refused one.
        env = Envelope(
            sender_id="user",
            sender_role="user",
            message_type=MessageType.PROPOSAL,
            payload=proposal,
            recipient_role=(
                "product_designer"
                if str(proposal.get("target_area", "")).lower() in self.USER_FACING_TARGETS
                else "architect"
            ),
        )
        await self.bus.publish("proposals", env)
        await refusals.resolve(self.bus.redis, refusal_id)
        logger.info(
            "Restarted refusal %s from its proposal (new thread %s)",
            refusal_id, env.thread_id[:8],
        )
        return web.json_response({"restarted": True, "thread_id": env.thread_id})

    async def _find_proposal_for_thread(self, thread_id: str) -> dict | None:
        """The proposal payload that started a thread, or None if it has aged out."""
        if not thread_id:
            return None
        try:
            entries = await self.bus.redis.xrange("stream:proposals")
        except Exception:
            return None
        for _mid, fields in entries or []:
            try:
                env = json.loads(fields.get("data") or "{}")
            except (json.JSONDecodeError, AttributeError):
                continue
            if env.get("thread_id") == thread_id:
                return env.get("payload") or None
        return None

    # ── Decision gates ─────────────────────────────────────

    # What stops on its own, and what stops for you. These rules already governed every
    # run, but only as lines in a config file nobody reads mid-flight — and not knowing
    # them produced exactly the alarm it should have prevented: work marked "approved"
    # with no approval given, because "approved" there meant the architect's decision and
    # nothing in the interface said so.
    _GATE_DESCRIPTIONS = {
        "assign_task": (
            "Before any work starts",
            "The architect has accepted a proposal and written a task. Nothing is built, "
            "no branch is created and no developer is given it until you approve. This is "
            "the cheap place to say no — refusing later means the work was already paid for.",
        ),
        "create_pr": (
            "Before a pull request is opened",
            "The work is done, committed on an agent/ branch and reviewed. Opening the PR "
            "is the first step that leaves this machine.",
        ),
        "delete_files": (
            "Before any file is deleted",
            "Deletion is not reversible by the pipeline that did it.",
        ),
        "modify_database": (
            "Before a database is modified",
            "Schema and data changes outlive the branch they were made on.",
        ),
        "push_to_remote": (
            "Before a branch is pushed",
            "Pushing publishes work to a remote others can see.",
        ),
        "large_change": (
            "When a change touches more files than the limit",
            "A change that spreads further than expected is usually a change that was "
            "understood differently by whoever proposed it.",
        ),
        "protected_file": (
            "Before a protected file is touched",
            "Only in dogfood mode; elsewhere a protected file is refused outright.",
        ),
    }

    async def _handle_policy(self, request: web.Request) -> web.Response:
        """The decision rules in force, so the interface can state them rather than imply them."""
        s = self.safety
        if s is None:
            return web.json_response({"available": False})

        required = list(getattr(s, "user_approval_required", []) or [])
        gates = []
        for action in required:
            when, why = self._GATE_DESCRIPTIONS.get(
                action, (action.replace("_", " "), "Requires approval before it happens."),
            )
            gates.append({"action": action, "when": when, "why": why})

        # Everything below happens WITHOUT asking, which is as important to state: a
        # reader who knows only what is gated cannot tell what is not.
        automatic = [
            "Reading the codebase, at every stage.",
            "Proposals, design feedback and technical review — these produce opinions, not changes.",
            "Committing to an agent/ branch inside an isolated worktree.",
            "Status and progress messages between agents.",
        ]

        return web.json_response({
            "available": True,
            "gates": gates,
            "automatic": automatic,
            "blocked_outright": list(getattr(s, "protected_files", []) or []),
            "never_push_to": list(getattr(s, "never_push_to", []) or []),
            "branch_prefix": getattr(s, "branch_prefix", ""),
            "max_files_per_change": getattr(s, "max_files_per_change", None),
            "gate_timeout_hours": round(self._gate_timeout_hours, 1) if self._gate_timeout_hours else None,
        })

    # ── Orchestration mode ─────────────────────────────────

    # Which mode is in force decides only ONE thing: whether the PM agent proposes work.
    # A user proposal is an ordinary proposal envelope and routes by target_area exactly
    # as the PM's would, so nothing downstream is mode-aware.

    async def _handle_mode_get(self, request: web.Request) -> web.Response:
        mode = await get_mode(self.bus.redis)
        return web.json_response({"mode": mode, "modes": list(MODES)})

    async def _handle_mode_set(self, request: web.Request) -> web.Response:
        """Switch modes while running. Behind the token: this decides who spends money."""
        if err := self._check_gate_auth(request):
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        requested = str(body.get("mode", "")).strip().lower()
        if requested not in MODES:
            return web.json_response(
                {"error": f"mode must be one of: {', '.join(MODES)}"}, status=400,
            )

        mode = await set_mode(self.bus.redis, requested)

        # Switching back to automatic WAKES THE PM. It stood down by backing off rather
        # than by dying, so it is still subscribed - but it is waiting on a message that
        # will never come unless something sends one. Without this, "automatic" would
        # appear to do nothing until the next restart.
        woken = False
        if mode == "automatic":
            try:
                await self.bus.publish("system", Envelope(
                    sender_id="dashboard", sender_role="user",
                    message_type=MessageType.SYSTEM,
                    payload={"action": "analyze_codebase",
                             "reason": "switched to automatic mode"},
                ))
                woken = True
            except Exception:
                logger.exception("Could not publish PM wake trigger")

        logger.info("Mode set to %s (pm_triggered=%s)", mode, woken)
        return web.json_response({"mode": mode, "pm_triggered": woken})

    # ── Manually submitted proposals ───────────────────────

    USER_FACING_TARGETS = {
        "product", "ux", "trust", "onboarding", "workflow", "adoption", "feature",
    }

    async def _handle_submit_proposal(self, request: web.Request) -> web.Response:
        """Publish a proposal written by a user, as the PM would have.

        Same envelope, same routing rule, same stream. The architect has never cared how
        a proposal arrived, which is why manual mode needs no changes downstream.

        Passing `thread_id` continues an existing thread — that is how a revision answers
        the architect's "needs more information" without starting a new flow.
        """
        if err := self._check_gate_auth(request):
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # NOTHING IS MANDATORY except that the proposal is not empty. The architect has
        # the codebase in front of it and can infer a great deal; where it cannot, asking
        # is a better use of its context than a form refusing to submit. The user is the
        # quality gate either way, so a thin proposal costs a question, not a defect.
        title = str(body.get("title", "")).strip()
        user_problem = str(body.get("user_problem", "")).strip()
        if not title and not user_problem:
            return web.json_response(
                {"error": "give it a title, or at least say what the problem is"},
                status=400,
            )
        if not title:
            # Still needs a handle: every view names a proposal by its title.
            title = user_problem.split("\n")[0][:120]

        target_area = str(body.get("target_area", "product")).strip().lower()
        payload = {
            "title": title,
            "target_area": target_area,
            "user_problem": user_problem,
            "description": str(body.get("description", "") or user_problem),
            "proposed_change": str(body.get("proposed_change", "")),
            "rationale": str(body.get("rationale", "")),
            "expected_user_outcome": str(body.get("expected_user_outcome", "")),
            "success_signal": str(body.get("success_signal", "")),
            "priority": int(body.get("priority", 2) or 2),
            "affected_files": list(body.get("affected_files", []) or []),
            "estimated_effort": str(body.get("estimated_effort", "medium")),
            "category": str(body.get("category", target_area)),
        }

        recipient = "product_designer" if target_area in self.USER_FACING_TARGETS else "architect"
        kwargs = dict(
            sender_id="user", sender_role="user",
            message_type=MessageType.PROPOSAL,
            payload=payload, recipient_role=recipient,
        )
        thread_id = str(body.get("thread_id", "") or "").strip()
        if thread_id:
            kwargs["thread_id"] = thread_id

        env = Envelope(**kwargs)
        await self.bus.publish("proposals", env)
        logger.info(
            "User proposal published to %s (thread %s): %s",
            recipient, env.thread_id[:8], title,
        )
        return web.json_response({
            "published": True,
            "thread_id": env.thread_id,
            "recipient_role": recipient,
        })

    async def _handle_orchestrator_status(self, request: web.Request) -> web.Response:
        """Is one running, and may this caller start one?

        `can_start` lets the UI show a disabled button with a reason rather than offering
        one that fails - a button that looks available and is not is worse than no button.
        """
        running = await is_running(self.bus.redis)
        # A start that died reads exactly like a start still in progress - no heartbeat
        # either way - so the UI sat on "starting" until its own timeout and never said
        # why. Only meaningful while not running: once the heartbeat is up, an earlier
        # failed attempt is history.
        failure = None if running else last_start_failure()
        return web.json_response({
            "running": running,
            "can_start": not running,
            "reason": "already running" if running else "",
            "failed": bool(failure),
            "error": failure or "",
            "auth_required": bool(self.gate_token),
        })

    async def _handle_orchestrator_start(self, request: web.Request) -> web.Response:
        """Start a detached orchestrator.

        Behind the same token as approve/deny, and for a stronger reason: this starts a
        process that will edit files, run commands and open pull requests. If anything on
        this dashboard needs authenticating, it is this.
        """
        if err := self._check_gate_auth(request):
            return err

        # Re-check under the request rather than trusting the UI's last poll. Two tabs,
        # or a stale page, would otherwise start two orchestrators against one Redis.
        if await is_running(self.bus.redis):
            return web.json_response(
                {"started": False, "error": "an orchestrator is already running"}, status=409,
            )

        # A previous attempt's failure must not describe this one.
        clear_start_failure()

        try:
            pid = spawn_orchestrator()
        except SupervisorError as exc:
            logger.error("orchestrator start failed: %s", exc)
            return web.json_response({"started": False, "error": str(exc)}, status=500)

        # Deliberately NOT waiting for the heartbeat here. Startup runs preflight, cleans
        # stale worktrees and connects every agent, which takes longer than a request
        # should block for. The UI polls the status endpoint instead, so a slow start
        # looks like a slow start rather than a failed request.
        logger.info("orchestrator started from dashboard, pid %s", pid)
        return web.json_response({"started": True, "pid": pid})

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
            for _mid, data in await r.xrange("stream:user-gates"):
                env = Envelope.from_json(data["data"])
                if env.id == gate_id:
                    thread_id = env.thread_id
                    found = True
                    break
        except Exception:
            pass

        if not found:
            return web.json_response({"error": "gate not found"}, status=404)

        response_env = Envelope(sender_id="user", sender_role="user", message_type=MessageType.SYSTEM, payload={"action": action, "gate_id": gate_id}, thread_id=thread_id)
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
            return web.json_response({"error": str(e)}, status=500)

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
            return web.json_response({"error": str(e)}, status=500)

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
