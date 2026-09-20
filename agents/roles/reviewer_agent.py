"""Reviewer Agent — reviews code changes, approves or requests changes."""

from __future__ import annotations

import logging

from agents.core.base_agent import AgentProcess
from agents.core.message import Envelope, MessageType
from agents.core.schema import parse_agent_output

logger = logging.getLogger(__name__)


class ReviewerAgent(AgentProcess):

    def default_channels(self) -> dict:
        return {
            "subscribes_to": ["review-requests"],
            "publishes_to": ["review-results", "cli-traces"],
        }

    def format_prompt(self, envelope: Envelope) -> str:
        if envelope.message_type == MessageType.REVIEW_REQUEST:
            req = envelope.payload
            branch = req.get("branch_name", "unknown")
            files = req.get("files_changed", [])[:10]
            tests_passed = req.get("tests_passed", False)
            tests_added = req.get("tests_added", [])
            notes = req.get("notes", "")

            prompt = (
                f"Review the code changes on branch '{branch}' as specified "
                f"in your system prompt.\n\n"
                f"Files changed: {', '.join(files)}\n"
                f"Tests passed: {tests_passed}\n"
            )
            if tests_added:
                prompt += f"Tests added: {', '.join(tests_added[:5])}\n"
            if notes:
                prompt += f"Developer notes: {notes}\n"
            prompt += (
                f"\nFirst, verify the branch exists by running: "
                f"`git show-ref --verify refs/heads/{branch}`\n"
                f"If the branch does NOT exist, output a JSON review_result with "
                f'decision "changes_requested" and summary "Branch {branch} not found '
                f'in this repository. The developer may not have created it yet."\n\n'
                f"If the branch exists, run `git diff main..{branch}` to see the actual changes.\n\n"
                f"After reviewing, output ONLY a single JSON object with your decision. "
                f"Do not narrate your review process. Start your response with the JSON."
            )
            return prompt

        return f"Process this message type: {envelope.message_type.value}"

    def parse_response(self, raw: str, source_envelope: Envelope) -> list[Envelope]:
        messages = parse_agent_output(raw)
        envelopes = []

        target_dev = source_envelope.sender_id

        for msg in messages:
            if msg.get("message_type") != "review_result":
                continue
            p = msg.get("payload", {})
            # The source envelope decides the thread, not the model. See pm_agent.
            tid = source_envelope.thread_id
            clean = {
                "decision": str(p.get("decision", "changes_requested")),
                "summary": str(p.get("summary", "")),
                "blocking_issues": [str(b) for b in p.get("blocking_issues", [])[:5]],
                "comments": [
                    {
                        "file": str(c.get("file", "")),
                        "line": c.get("line"),
                        "severity": str(c.get("severity", "")),
                        "comment": str(c.get("comment", "")),
                    }
                    for c in p.get("comments", [])[:10]
                ],
                "approval_note": str(p.get("approval_note", "")),
                "_target_agent_id": target_dev,
            }
            envelopes.append(Envelope(
                sender_id=self.agent_id,
                sender_role=self.role,
                message_type=MessageType.REVIEW_RESULT,
                payload=clean,
                thread_id=tid,
            ))

        # Narrative fallback: Codex sometimes produces prose instead of JSON
        if not envelopes and not messages:
            review = self._extract_from_narrative(raw)
            if review:
                review["_target_agent_id"] = target_dev
                envelopes.append(Envelope(
                    sender_id=self.agent_id,
                    sender_role=self.role,
                    message_type=MessageType.REVIEW_RESULT,
                    payload=review,
                    thread_id=source_envelope.thread_id,
                ))

        return envelopes

    @staticmethod
    def _extract_from_narrative(raw: str) -> dict | None:
        """Last-resort: extract decision from narrative text (Codex fallback)."""
        raw_lower = raw.lower()
        decision = None
        if "approve" in raw_lower and "changes_requested" not in raw_lower:
            decision = "approved"
        elif "changes_requested" in raw_lower or "request changes" in raw_lower or "blocking" in raw_lower:
            decision = "changes_requested"
        elif "lgtm" in raw_lower or "looks good" in raw_lower:
            decision = "approved"

        if decision:
            logger.info("Reviewer: extracted decision '%s' from narrative (no JSON found)", decision)
            issues = []
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") or stripped.startswith("* "):
                    issues.append(stripped[2:])
            return {
                "decision": decision,
                "summary": raw,
                "blocking_issues": issues[:5],
                "comments": [],
                "approval_note": "",
            }

        logger.warning("Reviewer: could not extract decision from response (%d chars)", len(raw))
        return None
