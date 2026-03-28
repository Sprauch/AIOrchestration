"""Architect Agent — reviews proposals, produces tech specs and task assignments."""

from __future__ import annotations

import logging

from agents.core.base_agent import AgentProcess, DeliberatingAgent
from agents.core.message import Envelope, MessageType
from agents.core.schema import parse_agent_output

logger = logging.getLogger(__name__)


class ArchitectAgent(AgentProcess):

    def default_channels(self) -> dict:
        return {
            "subscribes_to": ["proposals"],
            "publishes_to": ["reviews", "tasks", "cli-traces"],
        }

    def format_prompt(self, envelope: Envelope) -> str:
        if envelope.message_type == MessageType.PROPOSAL:
            p = envelope.payload
            return (
                f"Review this proposal and respond with your decision as specified "
                f"in your system prompt.\n\n"
                f"Title: {p.get('title', '?')}\n"
                f"Priority: {p.get('priority', '?')}\n"
                f"Category: {p.get('category', '?')}\n"
                f"Effort: {p.get('estimated_effort', '?')}\n"
                f"Affected files: {', '.join(p.get('affected_files', []))}\n"
                f"Description: {str(p.get('description', '?'))[:500]}"
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
