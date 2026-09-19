"""Product Designer Agent — reviews user-facing proposals for experience quality."""

from __future__ import annotations

import logging

from agents.core.base_agent import AgentProcess
from agents.core.message import Envelope, MessageType
from agents.core.schema import parse_agent_output

logger = logging.getLogger(__name__)


class ProductDesignerAgent(AgentProcess):

    USER_FACING_TARGETS = {"product", "ux", "trust", "onboarding", "workflow", "adoption", "feature"}

    def default_channels(self) -> dict:
        return {
            "subscribes_to": ["proposals"],
            "publishes_to": ["design-feedback", "cli-traces"],
        }

    def format_prompt(self, envelope: Envelope) -> str:
        if envelope.message_type == MessageType.PROPOSAL:
            p = envelope.payload
            return (
                "Review this proposal from a product-design perspective and respond "
                "with structured design feedback as specified in your system prompt.\n\n"
                f"Title: {p.get('title', '?')}\n"
                f"Target area: {p.get('target_area', p.get('category', '?'))}\n"
                f"User problem: {str(p.get('user_problem') or p.get('description', '?'))[:500]}\n"
                f"Proposed change: {str(p.get('proposed_change', '?'))[:500]}\n"
                f"Expected user outcome: {str(p.get('expected_user_outcome', '?'))[:300]}\n"
                f"Success signal: {str(p.get('success_signal', '?'))[:200]}\n"
                f"Affected files: {', '.join(p.get('affected_files', []))}"
            )
        return f"Process this message type: {envelope.message_type.value}"

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        messages = parse_agent_output(raw)
        feedback = None

        for msg in messages:
            if msg.get("message_type") != "design_feedback":
                continue
            p = msg.get("payload", {})
            feedback = {
                "summary": str(p.get("summary", ""))[:200],
                "user_experience_problem": str(p.get("user_experience_problem", ""))[:400],
                "design_goal": str(p.get("design_goal", ""))[:300],
                "recommendations": [str(r)[:250] for r in p.get("recommendations", [])[:6]],
                "priority": int(p.get("priority", source_envelope.payload.get("priority", 3)) or 3),
                "target_surfaces": [str(s)[:120] for s in p.get("target_surfaces", [])[:8]],
                "success_signal": str(p.get("success_signal", source_envelope.payload.get("success_signal", "")))[:200],
            }
            break

        if feedback is None:
            # Do not stall the pipeline if the model returns no structured feedback.
            feedback = {
                "summary": "No additional design concerns.",
                "user_experience_problem": "",
                "design_goal": "Proceed with the proposal while preserving clarity and usability.",
                "recommendations": [],
                "priority": int(source_envelope.payload.get("priority", 3) or 3),
                "target_surfaces": [],
                "success_signal": str(source_envelope.payload.get("success_signal", ""))[:200],
            }

        feedback["proposal"] = {
            "title": str(source_envelope.payload.get("title", ""))[:100],
            "target_area": str(source_envelope.payload.get("target_area", source_envelope.payload.get("category", "")))[:40],
            "user_problem": str(source_envelope.payload.get("user_problem", source_envelope.payload.get("description", "")))[:400],
            "description": str(source_envelope.payload.get("description", ""))[:400],
            "proposed_change": str(source_envelope.payload.get("proposed_change", ""))[:300],
            "rationale": str(source_envelope.payload.get("rationale", ""))[:300],
            "expected_user_outcome": str(source_envelope.payload.get("expected_user_outcome", ""))[:300],
            "success_signal": str(source_envelope.payload.get("success_signal", ""))[:200],
            "priority": source_envelope.payload.get("priority", 3),
            "affected_files": [str(f)[:100] for f in source_envelope.payload.get("affected_files", [])[:10]],
            "estimated_effort": str(source_envelope.payload.get("estimated_effort", "medium"))[:20],
            "category": str(source_envelope.payload.get("category", ""))[:30],
        }

        return [Envelope(
            sender_id=self.agent_id,
            sender_role=self.role,
            message_type=MessageType.DESIGN_FEEDBACK,
            payload=feedback,
            thread_id=source_envelope.thread_id,
            recipient_role="architect",
        )]
