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


# NOTHING HERE TRUNCATES PROSE.
#
# maxLength in a structured-output schema is not a validator that rejects an over-long
# string. It constrains DECODING: the model is stopped the moment it reaches the limit,
# mid-word, mid-sentence. A review concern capped at 300 arrived as
#
#   "...every install since the Angular 16 upgrade would ha"
#
# which is not a shorter review, it is an unreadable one. Those limits were in the schema
# from the initial import with no recorded reason, and never bit, because --json-schema
# was being dropped before it reached the CLI; fixing that made all of them real at once.
#
# THE TRADE, and it is not close. A complete message costs a few hundred tokens once. An
# incomplete one costs a reader - user or agent - guessing at the missing context while
# doing the work, and usually another round to recover what was cut. Letting prose run as
# long as the context requires is the cheap option; the expensive option is paying later
# for what was removed here.
#
# What still constrains output is maxItems: five concerns, six recommendations. Limiting
# the NUMBER makes a writer choose what matters, which is the useful pressure. Limiting
# the LENGTH only removes the end of the reasoning, which is not.
#
# The single exception below is thread_id, where a fixed width is a genuine contract - it
# is an id the system generated and the agent echoes back, and it is sliced for display.


def _str(max_len: int) -> dict:
    """A bounded string. Only for FORMAT fields, never for prose."""
    return {"type": "string", "maxLength": max_len}


def _text() -> dict:
    """Unbounded text. The default for anything a user reads."""
    return {"type": "string"}


def _text_array(max_items: int) -> dict:
    """A bounded NUMBER of unbounded entries — count is capped, content is not."""
    return {
        "type": "array",
        "items": _text(),
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
        # Unbounded like everything else. The prompt asks for a headline under 80
        # characters and says why; a list ellipsizes in CSS, where it knows how much room
        # it has. A title cut mid-word by the schema helped nobody.
        "title": _text(),
        "target_area": {"type": "string", "enum": ["product", "ux", "trust", "reliability", "technical", "cost", "onboarding", "workflow"]},
        "user_problem": _text(),
        "description": _text(),
        "proposed_change": _text(),
        "rationale": _text(),
        "expected_user_outcome": _text(),
        "success_signal": _text(),
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "affected_files": _text_array(10),
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
        "summary": _text(),
        "user_experience_problem": _text(),
        "design_goal": _text(),
        "recommendations": _text_array(6),
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "target_surfaces": _text_array(8),
        "success_signal": _text(),
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
        "concerns": _text_array(5),
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
        "approach": _text(),
        "branch_name": _text(),
        "files_to_modify": _text_array(10),
        "files_to_create": _text_array(10),
        "acceptance_criteria": _text_array(5),
        "testing_strategy": _text(),
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
        "branch_name": _text(),
        "changes_summary": _text(),
        "files_changed": _text_array(10),
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
        "branch_name": _text(),
        "files_changed": _text_array(10),
        "tests_passed": {"type": "boolean"},
        "tests_added": _text_array(10),
        "notes": _text(),
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
        "file": _text(),
        "line": {"type": ["integer", "null"]},
        "severity": _text(),
        "comment": _text(),
    },
    "required": ["file", "line", "severity", "comment"],
}

REVIEW_RESULT_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["approved", "changes_requested"]},
        "summary": _text(),
        "blocking_issues": _text_array(5),
        "comments": {
            "type": "array",
            "items": REVIEW_COMMENT,
            "maxItems": 10,
        },
        "approval_note": _text(),
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
    # NO "$schema" KEY. Declaring draft 2020-12 made the Claude CLI reject the schema
    # outright - `--json-schema is not a valid JSON Schema: no schema with key or ref
    # "https://json-schema.org/draft/2020-12/schema"` - because its validator cannot
    # resolve that meta-schema. Without the key the same schema is accepted and enforced,
    # and structured_output comes back conforming. The declaration bought nothing: no
    # consumer dispatched on it.
    return {
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
