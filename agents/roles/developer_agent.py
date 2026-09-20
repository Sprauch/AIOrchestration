"""Developer Agent — implements approved specs on agent/* branches."""

from __future__ import annotations

import logging

from agents.core.base_agent import AgentProcess
from agents.core.message import Envelope, MessageType
from agents.core.schema import parse_agent_output

logger = logging.getLogger(__name__)


class DeveloperAgent(AgentProcess):

    def default_channels(self) -> dict:
        return {
            "subscribes_to": ["tasks", "review-results"],
            "publishes_to": ["review-requests", "progress", "cli-traces"],
        }

    def format_prompt(self, envelope: Envelope) -> str:
        if envelope.message_type == MessageType.TASK_ASSIGNMENT:
            spec = envelope.payload
            prompt = (
                f"Implement the following task as specified in your system prompt.\n\n"
                f"Branch: {spec.get('branch_name', 'agent/unnamed')}\n"
                f"Files to modify: {', '.join(spec.get('files_to_modify', []))}\n"
                f"Files to create: {', '.join(spec.get('files_to_create', []))}\n"
            )
            approach = spec.get("approach", "")
            if approach:
                prompt += f"\nApproach: {approach}\n"
            prompt += (
                f"\nAcceptance criteria:\n"
                + "\n".join(f"  - {c}" for c in spec.get("acceptance_criteria", []))
                + f"\n\nTesting strategy: {spec.get('testing_strategy', '')}"
            )
            return prompt

        if envelope.message_type == MessageType.REVIEW_RESULT:
            decision = envelope.payload.get("decision", "")
            if decision == "changes_requested":
                blocking = envelope.payload.get("blocking_issues", [])[:5]
                comments = envelope.payload.get("comments", [])[:10]
                return (
                    f"The reviewer requested changes.\n\n"
                    f"Blocking issues:\n"
                    + "\n".join(f"  - {b}" for b in blocking)
                    + f"\n\nComments:\n"
                    + "\n".join(
                        f"  - {c.get('file', '?')}:{c.get('line', '?')} [{c.get('severity', '?')}] {c.get('comment', '')}"
                        for c in comments
                    )
                    + "\n\nFix these issues, run tests, and resubmit."
                )

        return f"Process this message type: {envelope.message_type.value}"

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        messages = parse_agent_output(raw)
        envelopes = []

        branch_fallback = source_envelope.payload.get("branch_name", "")

        for msg in messages:
            mt = msg.get("message_type")
            p = msg.get("payload", {})
            # The SOURCE envelope decides the thread, not the model. A downstream
            # message always continues the thread it is answering, and a model-supplied
            # id can only be an echo of what it was given or a hallucination - the first
            # adds nothing, the second silently moves work onto another thread.
            tid = source_envelope.thread_id
            branch = str(p.get("branch_name") or branch_fallback or "")

            if mt == "task_progress":
                envelopes.append(Envelope(
                    sender_id=self.agent_id,
                    sender_role=self.role,
                    message_type=MessageType.TASK_PROGRESS,
                    payload={
                        "status": str(p.get("status", "completed")),
                        "branch_name": branch,
                        "changes_summary": str(p.get("changes_summary", "")),
                        "files_changed": [str(f) for f in p.get("files_changed", [])[:10]],
                    },
                    thread_id=tid,
                ))

            elif mt == "review_request":
                envelopes.append(Envelope(
                    sender_id=self.agent_id,
                    sender_role=self.role,
                    message_type=MessageType.REVIEW_REQUEST,
                    payload={
                        "branch_name": branch,
                        "files_changed": [str(f) for f in p.get("files_changed", [])[:10]],
                        "tests_passed": bool(p.get("tests_passed", False)),
                        "tests_added": [str(t) for t in p.get("tests_added", [])[:10]],
                        "notes": str(p.get("notes", "")),
                    },
                    thread_id=tid,
                    recipient_role="reviewer",
                ))

        return envelopes
