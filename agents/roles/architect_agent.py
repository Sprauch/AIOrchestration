"""Architect Agent — reviews proposals, design feedback, and produces task assignments."""

from __future__ import annotations

import logging

from agents.core.base_agent import AgentProcess, DeliberatingAgent
from agents.core.message import Envelope, MessageType
from agents.core.schema import parse_agent_output

logger = logging.getLogger(__name__)


class ArchitectAgent(AgentProcess):

    def default_channels(self) -> dict:
        return {
            "subscribes_to": ["proposals", "design-feedback"],
            "publishes_to": ["reviews", "tasks", "cli-traces"],
        }

    def format_prompt(self, envelope: Envelope) -> str:
        if envelope.message_type == MessageType.PROPOSAL:
            p = envelope.payload
            return (
                "Review this proposal for technical feasibility, implementation shape, "
                "architectural fit, and delivery risk. Respond as specified in your system prompt.\n\n"
                f"Title: {p.get('title', '?')}\n"
                f"Target area: {p.get('target_area', p.get('category', '?'))}\n"
                f"Priority: {p.get('priority', '?')}\n"
                f"Effort: {p.get('estimated_effort', '?')}\n"
                f"User problem: {str(p.get('user_problem') or p.get('description', '?'))[:500]}\n"
                f"Proposed change: {str(p.get('proposed_change', '?'))[:500]}\n"
                f"Expected user outcome: {str(p.get('expected_user_outcome', '?'))[:300]}\n"
                f"Affected files: {', '.join(p.get('affected_files', []))}\n"
                f"Rationale: {str(p.get('rationale', '?'))[:300]}"
            )

        if envelope.message_type == MessageType.DESIGN_FEEDBACK:
            p = envelope.payload
            proposal = p.get("proposal", {}) if isinstance(p.get("proposal"), dict) else {}
            recommendations = p.get("recommendations", [])[:6]
            return (
                "Review this user-facing proposal with the attached product design feedback. "
                "Decide whether to approve, reject, or send back for revision, and produce "
                "a technical task assignment if approved.\n\n"
                f"Title: {proposal.get('title', '?')}\n"
                f"Target area: {proposal.get('target_area', proposal.get('category', '?'))}\n"
                f"Priority: {proposal.get('priority', '?')}\n"
                f"Effort: {proposal.get('estimated_effort', '?')}\n"
                f"User problem: {str(proposal.get('user_problem') or proposal.get('description', '?'))[:500]}\n"
                f"Proposed change: {str(proposal.get('proposed_change', '?'))[:500]}\n"
                f"Expected user outcome: {str(proposal.get('expected_user_outcome', '?'))[:300]}\n"
                f"Affected files: {', '.join(proposal.get('affected_files', []))}\n\n"
                f"Design summary: {str(p.get('summary', ''))[:200]}\n"
                f"Experience problem: {str(p.get('user_experience_problem', ''))[:400]}\n"
                f"Design goal: {str(p.get('design_goal', ''))[:300]}\n"
                f"Recommendations:\n" + "\n".join(f"  - {str(r)[:250]}" for r in recommendations)
            )

        return f"Process this message type: {envelope.message_type.value}"

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        messages = parse_agent_output(raw)
        envelopes = []

        for msg in messages:
            mt = msg.get("message_type")
            p = msg.get("payload", {})
            tid = msg.get("thread_id") or source_envelope.thread_id

            if mt == "proposal_review":
                clean = {
                    "decision": str(p.get("decision", ""))[:30],
                    "concerns": [str(c)[:300] for c in p.get("concerns", [])[:5]],
                }
                envelopes.append(Envelope(
                    sender_id=self.agent_id,
                    sender_role=self.role,
                    message_type=MessageType.PROPOSAL_REVIEW,
                    payload=clean,
                    thread_id=tid,
                    recipient_role="pm",
                ))

            elif mt == "task_assignment":
                clean = {
                    "approach": str(p.get("approach", ""))[:500],
                    "branch_name": str(p.get("branch_name", ""))[:80],
                    "files_to_modify": [str(f)[:100] for f in p.get("files_to_modify", [])[:10]],
                    "files_to_create": [str(f)[:100] for f in p.get("files_to_create", [])[:10]],
                    "acceptance_criteria": [str(c)[:200] for c in p.get("acceptance_criteria", [])[:5]],
                    "testing_strategy": str(p.get("testing_strategy", ""))[:200],
                }
                envelopes.append(Envelope(
                    sender_id=self.agent_id,
                    sender_role=self.role,
                    message_type=MessageType.TASK_ASSIGNMENT,
                    payload=clean,
                    thread_id=tid,
                    recipient_role="developer",
                ))

        return envelopes


class ArchitectDeliberatingAgent(DeliberatingAgent, ArchitectAgent):
    def default_channels(self) -> dict:
        return ArchitectAgent.default_channels(self)

    def format_prompt(self, envelope: Envelope) -> str:
        return ArchitectAgent.format_prompt(self, envelope)

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        return ArchitectAgent.parse_response(self, raw, source_envelope)
