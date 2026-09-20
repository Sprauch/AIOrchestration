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
                    + "\n".join(f"  - {str(c)}" for c in concerns)
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
                "title": str(p.get("title", "")),
                "target_area": str(p.get("target_area", p.get("category", "")))[:40],
                "user_problem": str(p.get("user_problem", "")),
                "description": str(description),
                "proposed_change": str(p.get("proposed_change", "")),
                "rationale": str(p.get("rationale", "")),
                "expected_user_outcome": str(p.get("expected_user_outcome", "")),
                "success_signal": str(p.get("success_signal", "")),
                "priority": p.get("priority", 3),
                "affected_files": p.get("affected_files", [])[:10],
                "estimated_effort": str(p.get("estimated_effort", "medium")),
                "category": str(p.get("category", "quality")),
            }
            recipient_role = "product_designer" if clean["target_area"] in self.USER_FACING_TARGETS else "architect"
            # THREAD IDENTITY IS NOT THE MODEL'S TO DECIDE.
            #
            # A proposal is the ROOT of its own flow, so it starts a new thread. A
            # revision answering architect feedback stays on the thread it revises. The
            # model's own thread_id is ignored here, because it has no way to know which
            # of those two things it is doing.
            #
            # It used to be preferred, and the effect was that nothing could be followed.
            # The schema REQUIRES thread_id on every message and _build_cli_prompt injects
            # THREAD_ID: <incoming>, so the PM dutifully echoed the incoming id onto every
            # proposal in a batch, and the "fresh thread per proposal" branch was
            # unreachable whenever the model did as it was told. One PM turn emitted nine
            # proposals on one thread; every descendant inherited it; one thread ended up
            # holding 59 messages spanning fifteen unrelated pieces of work. No view can
            # untangle that, because the grouping key genuinely is the same.
            tid = source_envelope.thread_id if preserve_thread else None

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
