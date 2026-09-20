"""Role-specific JSON Schemas for agent workflow output.

These schemas are enforced at the CLI boundary for the primary agent turn:
- Claude uses `--json-schema`
- Codex exec uses `--output-schema`

All roles share the same outer envelope:
{
  "schema_version": 1,
  "messages": [ ... ]
}
"""

from __future__ import annotations

from copy import deepcopy


def _str(max_len: int) -> dict:
    return {"type": "string", "maxLength": max_len}


def _str_array(max_items: int, max_len: int) -> dict:
    return {
        "type": "array",
        "items": _str(max_len),
        "maxItems": max_items,
    }


MESSAGE_BASE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "message_type": {"type": "string"},
        "recipient_role": {"type": ["string", "null"]},
        "thread_id": _str(32),
        "payload": {"type": "object"},
    },
    "required": ["message_type", "recipient_role", "thread_id", "payload"],
}


PROPOSAL_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        # 100 not 80: the prompt asks for 80 so there is headroom before the schema
        # rejects an otherwise good proposal. The API does not truncate, so the only
        # enforcement is here and in the prompt.
        "title": _str(100),
        "target_area": {"type": "string", "enum": ["product", "ux", "trust", "reliability", "technical", "cost", "onboarding", "workflow"]},
        "user_problem": _str(400),
        "description": _str(400),
        "proposed_change": _str(300),
        "rationale": _str(300),
        "expected_user_outcome": _str(300),
        "success_signal": _str(200),
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "affected_files": _str_array(10, 100),
        "estimated_effort": {"type": "string", "enum": ["small", "medium", "large"]},
        "category": {"type": "string", "enum": ["product", "ux", "trust", "workflow", "adoption", "reliability", "technical", "cost", "onboarding", "feature"]},
    },
    # API requires every property key to be in required.
    "required": ["title", "target_area", "user_problem", "description", "proposed_change", "rationale",
                 "expected_user_outcome", "success_signal", "priority", "affected_files", "estimated_effort", "category"],
}


PROPOSAL_MESSAGE = deepcopy(MESSAGE_BASE)
PROPOSAL_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "proposal"}
PROPOSAL_MESSAGE["properties"]["recipient_role"] = {
    "type": "string",
    "enum": ["architect", "product_designer"],
}
PROPOSAL_MESSAGE["properties"]["payload"] = PROPOSAL_PAYLOAD


DESIGN_FEEDBACK_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": _str(200),
        "user_experience_problem": _str(400),
        "design_goal": _str(300),
        "recommendations": _str_array(6, 250),
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "target_surfaces": _str_array(8, 120),
        "success_signal": _str(200),
    },
    "required": ["summary", "user_experience_problem", "design_goal", "recommendations", "priority", "target_surfaces", "success_signal"],
}

DESIGN_FEEDBACK_MESSAGE = deepcopy(MESSAGE_BASE)
DESIGN_FEEDBACK_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "design_feedback"}
DESIGN_FEEDBACK_MESSAGE["properties"]["recipient_role"] = {"type": "string", "const": "architect"}
DESIGN_FEEDBACK_MESSAGE["properties"]["payload"] = DESIGN_FEEDBACK_PAYLOAD


PROPOSAL_REVIEW_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["approved", "rejected", "needs_revision", "needs_clarification"]},
        "concerns": _str_array(5, 300),
    },
    "required": ["decision", "concerns"],
}

PROPOSAL_REVIEW_MESSAGE = deepcopy(MESSAGE_BASE)
PROPOSAL_REVIEW_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "proposal_review"}
PROPOSAL_REVIEW_MESSAGE["properties"]["recipient_role"] = {"type": "string", "const": "pm"}
PROPOSAL_REVIEW_MESSAGE["properties"]["payload"] = PROPOSAL_REVIEW_PAYLOAD


TASK_ASSIGNMENT_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "approach": _str(500),
        "branch_name": _str(80),
        "files_to_modify": _str_array(10, 100),
        "files_to_create": _str_array(10, 100),
        "acceptance_criteria": _str_array(5, 200),
        "testing_strategy": _str(200),
    },
    "required": ["approach", "branch_name", "files_to_modify", "files_to_create", "acceptance_criteria", "testing_strategy"],
}

TASK_ASSIGNMENT_MESSAGE = deepcopy(MESSAGE_BASE)
TASK_ASSIGNMENT_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "task_assignment"}
TASK_ASSIGNMENT_MESSAGE["properties"]["recipient_role"] = {"type": "string", "const": "developer"}
TASK_ASSIGNMENT_MESSAGE["properties"]["payload"] = TASK_ASSIGNMENT_PAYLOAD


TASK_PROGRESS_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "in_progress"]},
        "branch_name": _str(80),
        "changes_summary": _str(300),
        "files_changed": _str_array(10, 100),
    },
    "required": ["status", "branch_name", "changes_summary", "files_changed"],
}

TASK_PROGRESS_MESSAGE = deepcopy(MESSAGE_BASE)
TASK_PROGRESS_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "task_progress"}
TASK_PROGRESS_MESSAGE["properties"]["recipient_role"] = {"type": "null"}
TASK_PROGRESS_MESSAGE["properties"]["payload"] = TASK_PROGRESS_PAYLOAD


REVIEW_REQUEST_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "branch_name": _str(80),
        "files_changed": _str_array(10, 100),
        "tests_passed": {"type": "boolean"},
        "tests_added": _str_array(10, 100),
        "notes": _str(300),
    },
    "required": ["branch_name", "files_changed", "tests_passed", "tests_added", "notes"],
}

REVIEW_REQUEST_MESSAGE = deepcopy(MESSAGE_BASE)
REVIEW_REQUEST_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "review_request"}
REVIEW_REQUEST_MESSAGE["properties"]["recipient_role"] = {"type": "string", "const": "reviewer"}
REVIEW_REQUEST_MESSAGE["properties"]["payload"] = REVIEW_REQUEST_PAYLOAD


REVIEW_COMMENT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "file": _str(100),
        "line": {"type": ["integer", "null"]},
        "severity": _str(20),
        "comment": _str(200),
    },
    "required": ["file", "line", "severity", "comment"],
}

REVIEW_RESULT_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["approved", "changes_requested"]},
        "summary": _str(300),
        "blocking_issues": _str_array(5, 200),
        "comments": {
            "type": "array",
            "items": REVIEW_COMMENT,
            "maxItems": 10,
        },
        "approval_note": _str(300),
    },
    # API requires every property key to be in required.
    "required": ["decision", "summary", "blocking_issues", "comments", "approval_note"],
}

REVIEW_RESULT_MESSAGE = deepcopy(MESSAGE_BASE)
REVIEW_RESULT_MESSAGE["properties"]["message_type"] = {"type": "string", "const": "review_result"}
REVIEW_RESULT_MESSAGE["properties"]["recipient_role"] = {"type": "string", "const": "developer"}
REVIEW_RESULT_MESSAGE["properties"]["payload"] = REVIEW_RESULT_PAYLOAD


def _schema_for_messages(messages: list[dict]) -> dict:
    if len(messages) == 1:
        items_schema = messages[0]
    else:
        message_types = sorted({
            m["properties"]["message_type"]["const"]
            for m in messages
            if "const" in m.get("properties", {}).get("message_type", {})
        })
        recipient_roles = sorted({
            m["properties"]["recipient_role"]["const"]
            for m in messages
            if "const" in m.get("properties", {}).get("recipient_role", {})
        })
        items_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "message_type": {"type": "string", "enum": message_types},
                "recipient_role": {"type": "string", "enum": recipient_roles},
                "thread_id": _str(32),
                # Keep payload generic for multi-message roles to avoid unsupported
                # schema combinators like oneOf/anyOf in Codex response schemas.
                "payload": {"type": "object"},
            },
            "required": ["message_type", "recipient_role", "thread_id", "payload"],
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "messages": {
                "type": "array",
                "items": items_schema,
                "maxItems": 20,
            },
        },
        "required": ["schema_version", "messages"],
    }


ROLE_OUTPUT_SCHEMAS = {
    "pm": _schema_for_messages([PROPOSAL_MESSAGE]),
    "product_designer": _schema_for_messages([DESIGN_FEEDBACK_MESSAGE]),
    "architect": _schema_for_messages([PROPOSAL_REVIEW_MESSAGE, TASK_ASSIGNMENT_MESSAGE]),
    "developer": _schema_for_messages([TASK_PROGRESS_MESSAGE, REVIEW_REQUEST_MESSAGE]),
    "reviewer": _schema_for_messages([REVIEW_RESULT_MESSAGE]),
}


def get_output_schema_for_role(role: str) -> dict | None:
    """Return the JSON Schema for a primary agent role, or None if unknown."""
    schema = ROLE_OUTPUT_SCHEMAS.get(role)
    return deepcopy(schema) if schema else None
