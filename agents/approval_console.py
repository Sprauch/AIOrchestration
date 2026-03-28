"""Interactive console for handling human approval gates."""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

from agents.core.message import Envelope, MessageType
from agents.core.message_bus import MessageBus

logger = logging.getLogger(__name__)


class HumanApprovalConsole:
    """Interactive console for handling human approval gates.

    On startup, replays all existing pending gates from stream history
    so gates published before the console started are not missed.
    Then watches for new gates in real-time.

    Responses are published to a per-gate channel (stream:gate-responses:{gate_id})
    which the requesting agent blocks on via XREAD. Also published to
    stream:system for audit/observability.
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.bus = MessageBus(redis_url)
        self._handled_gate_ids: set[str] = set()

    async def run(self) -> None:
        await self.bus.connect()
        print("\n=== Human Approval Console ===")

        await self._replay_pending_gates()

        print("Watching for new approval requests...\n")

        try:
            async for envelope in self.bus.subscribe_simple(["human-gates"]):
                if envelope.id not in self._handled_gate_ids:
                    await self._handle_gate(envelope)
        except asyncio.CancelledError:
            pass
        finally:
            await self.bus.close()

    async def _replay_pending_gates(self) -> None:
        """Check stream history for gates that were never responded to."""
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            gate_messages = []
            try:
                raw = await r.xrange("stream:human-gates")
                for msg_id, data in raw:
                    try:
                        env = Envelope.from_json(data["data"])
                        gate_messages.append(env)
                    except Exception:
                        logger.warning("gate message parse failed for msg %s", msg_id, exc_info=True)
                        continue
            except Exception:
                logger.warning("approval stream read failed for stream:human-gates", exc_info=True)
                return

            if not gate_messages:
                print("  No pending gates.\n")
                return

            # Check which gates already have responses
            responded_gate_ids: set[str] = set()
            try:
                raw = await r.xrange("stream:system")
                for msg_id, data in raw:
                    try:
                        env = Envelope.from_json(data["data"])
                        action = env.payload.get("action", "")
                        gate_id = env.payload.get("gate_id", "")
                        if action in ("approval_granted", "approval_denied") and gate_id:
                            responded_gate_ids.add(gate_id)
                    except Exception:
                        logger.warning("system message parse failed for msg %s", msg_id, exc_info=True)
                        continue
            except Exception:
                logger.warning("approval stream read failed for stream:system", exc_info=True)

            pending = [g for g in gate_messages if g.id not in responded_gate_ids]
            if pending:
                print(f"  Found {len(pending)} pending gate(s) from history:\n")
                for gate in pending:
                    await self._handle_gate(gate)
            else:
                print("  All historical gates have been responded to.\n")

        finally:
            await r.aclose()

    @staticmethod
    def _format_context(context) -> str:
        """Format gate context for terminal display.

        Handles structured dicts, legacy strings, None, and empty values.
        """
        if not context:
            return ""
        if isinstance(context, str):
            return context
        if not isinstance(context, dict):
            return str(context)

        lines = []
        if op := context.get("operation"):
            lines.append(f"  Operation:   {op}")
        if branch := context.get("branch"):
            lines.append(f"  Branch:      {branch}")
        if (fc := context.get("file_count")) is not None:
            lines.append(f"  File count:  {fc}")
        if files := context.get("files"):
            for f in files[:10]:
                flag = " [PROTECTED]" if f.get("protected") else ""
                lines.append(f"    - {f.get('path', '?')}{flag}")
            if len(files) > 10:
                lines.append(f"    ... and {len(files) - 10} more")
        if summary := context.get("safety_summary"):
            lines.append(f"  Safety:      {summary}")
        if esc := context.get("escalation_reason"):
            lines.append(f"  Escalation:  {esc}")
        # Render any extra keys not already handled
        _known = {"operation", "branch", "file_count", "files", "safety_summary", "escalation_reason"}
        for k, v in context.items():
            if k not in _known:
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    async def _handle_gate(self, env: Envelope) -> None:
        action = env.payload.get("action", "unknown")
        reason = env.payload.get("reason", "No reason given")
        context = env.payload.get("context", "")

        print(f"\n{'=' * 50}")
        print(f"APPROVAL REQUIRED")
        print(f"From: {env.sender_id} ({env.sender_role})")
        print(f"Action: {action}")
        print(f"Reason: {reason}")
        formatted = self._format_context(context)
        if formatted:
            print(f"Context:\n{formatted}" if "\n" in formatted else f"Context: {formatted}")
        print(f"{'=' * 50}")

        while True:
            response = await asyncio.to_thread(
                input, "\nApprove? [y/n]: "
            )
            response = response.strip().lower()

            if response in ("y", "yes"):
                await self._respond(env, "approval_granted")
                print("Approved.\n")
                break
            elif response in ("n", "no"):
                await self._respond(env, "approval_denied")
                print("Denied.\n")
                break

    async def _respond(self, gate_env: Envelope, action: str) -> None:
        """Publish the approval response to both the per-gate channel and system stream."""
        response = Envelope(
            sender_id="human",
            sender_role="human",
            message_type=MessageType.SYSTEM,
            payload={"action": action, "gate_id": gate_env.id},
            thread_id=gate_env.thread_id,
        )

        # Per-gate channel — the agent is blocking on this via XREAD
        gate_channel = f"gate-responses:{gate_env.id}"
        await self.bus.publish(gate_channel, response)

        # System stream — for audit and observability
        await self.bus.publish("system", response)

        self._handled_gate_ids.add(gate_env.id)
