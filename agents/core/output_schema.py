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


# LENGTH BUDGETS.
#
# maxLength in a structured-output schema is not a validator that rejects an over-long
# string. It constrains DECODING: the model is stopped the moment it reaches the limit,
# mid-word, mid-sentence. A review concern capped at 300 arrived as
#
#   "...every install since the Angular 16 upgrade would ha"
#
# which is not a shorter review, it is an unreadable one. The limits below had been in
# the schema from the start and never bit, because --json-schema was being silently
# dropped before it reached the CLI; fixing that made every one of them real at once.
#
# So the budget is stated in the PROMPTS, where a model can compose to fit and finish its
# sentence, and the numbers here are HEADROOM - large enough that hitting one means
# something has gone wrong, not that a thought was long. Nothing else caps payload size
# (max_payload_bytes is declared in config and never read), so headroom is not unbounded.
# These are RUNAWAY GUARDS, set far above any real answer, not budgets. Cutting a concern
# short has no benefit worth having: the tokens saved are trivial beside what the architect
# spent deriving it, and a severed concern forces another round that costs more than the
# text it saved. What usefully forces prioritisation is maxItems - five concerns, six
# recommendations - because limiting the NUMBER makes a writer choose, where limiting the
# LENGTH only removes the end of the reasoning. The prompts state the working budget
# (~900 characters for a point); these numbers exist only so a pathological loop cannot
# emit megabytes, since nothing else caps payload size.
BRIEF = 1000       # a signal, a goal, a one-line outcome        (prompts ask for ~300)
POINT = 3000       # one complete point: concern, recommendation (prompts ask for ~900)
NARRATIVE = 5000   # the longest prose any role writes: approach (prompts ask for ~1,600)

# Short FORMAT fields keep tight limits, because for these the length IS the contract:
# a branch name, a file path, a severity word, an id.


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
        "user_problem": _str(POINT),
        "description": _str(POINT),
        "proposed_change": _str(POINT),
        "rationale": _str(POINT),
        "expected_user_outcome": _str(POINT),
        "success_signal": _str(BRIEF),
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
        "summary": _str(POINT),
        "user_experience_problem": _str(POINT),
        "design_goal": _str(POINT),
        "recommendations": _str_array(6, POINT),
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "target_surfaces": _str_array(8, 120),
        "success_signal": _str(BRIEF),
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
        "concerns": _str_array(5, POINT),
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
        "approach": _str(NARRATIVE),
        "branch_name": _str(80),
        "files_to_modify": _str_array(10, 100),
        "files_to_create": _str_array(10, 100),
        "acceptance_criteria": _str_array(5, BRIEF),
        "testing_strategy": _str(POINT),
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
        "changes_summary": _str(POINT),
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
        "notes": _str(POINT),
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
        "comment": _str(POINT),
    },
    "required": ["file", "line", "severity", "comment"],
}

REVIEW_RESULT_PAYLOAD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["approved", "changes_requested"]},
        "summary": _str(POINT),
        "blocking_issues": _str_array(5, 200),
        "comments": {
            "type": "array",
            "items": REVIEW_COMMENT,
            "maxItems": 10,
        },
        "approval_note": _str(POINT),
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
