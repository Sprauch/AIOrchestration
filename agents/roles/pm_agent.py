"""PM Agent — analyzes the codebase and produces improvement proposals."""

from __future__ import annotations

import logging

from agents.core.base_agent import AgentProcess, DeliberatingAgent
from agents.core.message import Envelope, MessageType
from agents.core.schema import parse_agent_output

logger = logging.getLogger(__name__)


class PMAgent(AgentProcess):
    USER_FACING_TARGETS = {"product", "ux", "trust", "onboarding", "workflow", "adoption", "feature"}

    def default_channels(self) -> dict:
        return {
            "subscribes_to": ["system", "reviews"],
            "publishes_to": ["proposals", "cli-traces"],
        }

    def format_prompt(self, envelope: Envelope) -> str:
        if envelope.message_type == MessageType.SYSTEM:
            action = envelope.payload.get("action", "")
            if action == "trigger_analysis":
                base = (
                    "Analyze the codebase and identify improvement opportunities. "
                    "Respond with a JSON block containing your proposals as specified "
                    "in your system prompt."
                )
                focus = envelope.payload.get("product_focus", "")
                if focus:
                    base = f"Product stakeholder priorities:\n{focus}\n\nFocus your proposals on these priorities.\n\n{base}"
                context = envelope.payload.get("analysis_context", "")
                if context:
                    base = f"{context}\n\n{base}"
                return base

        if envelope.message_type == MessageType.PROPOSAL_REVIEW:
            decision = envelope.payload.get("decision", "")
            if decision in ("needs_revision", "needs_clarification", "rejected"):
                concerns = envelope.payload.get("concerns", [])[:5]
                return (
                    f"The architect requested revisions.\n"
                    f"Decision: {decision}\n"
                    f"Concerns:\n"
                    + "\n".join(f"  - {str(c)[:300]}" for c in concerns)
                    + "\n\nRevise and resubmit your proposals as specified in your system prompt."
                )

        return f"Process this message type: {envelope.message_type.value}"

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        """Pure parser — no logging, no side effects."""
        messages = parse_agent_output(raw)
        envelopes = []

        preserve_thread = source_envelope.message_type == MessageType.PROPOSAL_REVIEW

        for msg in messages:
            if msg.get("message_type") != "proposal":
                continue
            p = msg.get("payload", {})
            description = p.get("user_problem") or p.get("description", "")
            clean = {
                "title": str(p.get("title", ""))[:100],
                "target_area": str(p.get("target_area", p.get("category", "")))[:40],
                "user_problem": str(p.get("user_problem", ""))[:400],
                "description": str(description)[:400],
                "proposed_change": str(p.get("proposed_change", ""))[:300],
                "rationale": str(p.get("rationale", ""))[:300],
                "expected_user_outcome": str(p.get("expected_user_outcome", ""))[:300],
                "success_signal": str(p.get("success_signal", ""))[:200],
                "priority": p.get("priority", 3),
                "affected_files": p.get("affected_files", [])[:10],
                "estimated_effort": str(p.get("estimated_effort", "medium"))[:20],
                "category": str(p.get("category", "quality"))[:30],
            }
            recipient_role = "product_designer" if clean["target_area"] in self.USER_FACING_TARGETS else "architect"
            # Thread ID resolution:
            # 1. If the agent's output specifies thread_id, use it
            # 2. If this is a revision (architect feedback), preserve source thread
            # 3. Otherwise, let Envelope generate a fresh thread_id per proposal
            tid = msg.get("thread_id")
            if not tid and preserve_thread:
                tid = source_envelope.thread_id

            kwargs = dict(
                sender_id=self.agent_id,
                sender_role=self.role,
                message_type=MessageType.PROPOSAL,
                payload=clean,
                recipient_role=recipient_role,
            )
            if tid:
                kwargs["thread_id"] = tid
            envelopes.append(Envelope(**kwargs))

        return envelopes


class PMDeliberatingAgent(DeliberatingAgent, PMAgent):
    def default_channels(self) -> dict:
        return PMAgent.default_channels(self)

    def format_prompt(self, envelope: Envelope) -> str:
        return PMAgent.format_prompt(self, envelope)

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        return PMAgent.parse_response(self, raw, source_envelope)
