"""Unified agent output schema — one parser for all roles.

Every agent outputs:
{
  "schema_version": 1,
  "messages": [
    {
      "message_type": "proposal|proposal_review|task_assignment|...",
      "recipient_role": "pm|architect|developer|reviewer|null",
      "thread_id": "optional",
      "payload": { ... role-specific ... }
    }
  ]
}

The runtime (base_agent) owns transport fields: sender_id, sender_role,
timestamp, id. Agents only set message_type, recipient_role, payload.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

VALID_MESSAGE_TYPES = {
    "proposal", "proposal_review", "task_assignment",
    "task_progress", "review_request", "review_result", "system",
}

VALID_ROLES = {"pm", "architect", "developer", "reviewer", None}


def parse_agent_output(raw: str) -> list[dict]:
    """Parse unified agent output. Returns list of message dicts.

    Tries multiple extraction strategies:
    1. Fenced ```json blocks
    2. Raw JSON (entire response)
    3. Embedded JSON in narrative (first { to last })

    Also accepts legacy per-role formats as fallback:
    - {"proposals": [...]} → converts to messages
    - {"decision": ...} → converts to single message
    - {"status": ...} → converts to single message

    Returns empty list on failure.
    """
    data = _extract_json(raw)
    if data is None:
        return []

    # Unified format: {"messages": [...]} or {"schema_version": N, "messages": [...]}
    if isinstance(data, dict) and "messages" in data:
        sv = data.get("schema_version")
        if sv is not None and sv != SCHEMA_VERSION:
            logger.warning(
                "Rejecting agent output: schema_version %s != expected %s",
                sv, SCHEMA_VERSION,
            )
            return []
        messages = data["messages"]
        if isinstance(messages, list):
            return [m for m in messages if _validate_message(m)]
        return []

    # Legacy fallback: convert old per-role formats
    return _convert_legacy(data)


def _extract_json(raw: str) -> dict | list | None:
    """Extract first valid JSON object or array from CLI output."""
    # 1. Fenced ```json blocks
    for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    # 2. Entire response as JSON
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass

    # 3. Embedded JSON — find first { or [ and try progressively
    for match in re.finditer(r'[\[{]', raw):
        candidate = raw[match.start():]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # Try truncating trailing narrative
        close = '}' if candidate[0] == '{' else ']'
        for i in range(len(candidate) - 1, 0, -1):
            if candidate[i] == close:
                try:
                    return json.loads(candidate[:i + 1])
                except json.JSONDecodeError:
                    pass
                break

    return None


def _validate_message(msg: dict) -> bool:
    """Validate a message dict against the unified schema contract."""
    if not isinstance(msg, dict):
        return False
    mt = msg.get("message_type")
    if mt not in VALID_MESSAGE_TYPES:
        logger.warning("Invalid message_type: %s (expected one of %s)", mt, VALID_MESSAGE_TYPES)
        return False
    if "payload" not in msg or not isinstance(msg["payload"], dict):
        logger.warning("Message missing payload dict (type=%s)", mt)
        return False
    role = msg.get("recipient_role")
    if role is not None and role not in VALID_ROLES:
        logger.warning("Invalid recipient_role: %s (expected one of %s)", role, VALID_ROLES)
        return False
    return True


def _convert_legacy(data) -> list[dict]:
    """Convert old per-role JSON formats to unified messages list."""

    # PM legacy: {"proposals": [...]}
    if isinstance(data, dict) and "proposals" in data:
        proposals = data["proposals"]
        if isinstance(proposals, list):
            return [
                {
                    "message_type": "proposal",
                    "recipient_role": "architect",
                    "payload": p,
                }
                for p in proposals
                if isinstance(p, dict)
            ]

    # Bare array of proposals: [{...}, {...}]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "title" in data[0]:
            return [
                {"message_type": "proposal", "recipient_role": "architect", "payload": p}
                for p in data if isinstance(p, dict)
            ]

    # Single proposal: {"title": ...}
    if isinstance(data, dict) and "title" in data and "decision" not in data:
        return [{"message_type": "proposal", "recipient_role": "architect", "payload": data}]

    # Architect legacy: {"decision": ..., "technical_spec": ...}
    # Reviewer legacy: {"decision": ..., "comments": [...], "blocking_issues": [...]}
    if isinstance(data, dict) and "decision" in data and "status" not in data:
        # Distinguish architect (has technical_spec or concerns) from reviewer (has comments/blocking_issues/summary)
        is_reviewer = "comments" in data or "blocking_issues" in data or "summary" in data
        if is_reviewer:
            return [{
                "message_type": "review_result",
                "recipient_role": "developer",
                "payload": data,
            }]

        messages = [
            {
                "message_type": "proposal_review",
                "recipient_role": "pm",
                "payload": {
                    "decision": data.get("decision"),
                    "concerns": data.get("concerns", []),
                },
            }
        ]
        if data.get("decision") == "approved" and data.get("technical_spec"):
            messages.append({
                "message_type": "task_assignment",
                "recipient_role": "developer",
                "payload": data["technical_spec"],
            })
        return messages

    # Developer legacy: {"status": ..., "branch_name": ...}
    if isinstance(data, dict) and "status" in data:
        messages = [
            {
                "message_type": "task_progress",
                "recipient_role": None,
                "payload": {
                    "status": data.get("status"),
                    "branch_name": data.get("branch_name", ""),
                    "changes_summary": data.get("changes_summary", ""),
                    "files_changed": data.get("files_changed", []),
                },
            }
        ]
        if data.get("status") == "completed":
            messages.append({
                "message_type": "review_request",
                "recipient_role": "reviewer",
                "payload": {
                    "branch_name": data.get("branch_name", ""),
                    "files_changed": data.get("files_changed", []),
                    "tests_passed": data.get("tests_passed", False),
                    "tests_added": data.get("tests_added", []),
                    "notes": data.get("notes", ""),
                },
            })
        return messages

    return []
