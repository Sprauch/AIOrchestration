"""Tests for unified agent output schema parsing and validation."""

from agents.core.schema import parse_agent_output, _validate_message, _extract_json, _convert_legacy


# ── Unified format ──

def test_unified_format_basic():
    raw = '{"schema_version": 1, "messages": [{"message_type": "proposal", "recipient_role": "architect", "payload": {"title": "Test"}}]}'
    result = parse_agent_output(raw)
    assert len(result) == 1
    assert result[0]["message_type"] == "proposal"
    assert result[0]["payload"]["title"] == "Test"


def test_unified_format_multiple_messages():
    raw = '{"schema_version": 1, "messages": [{"message_type": "proposal_review", "recipient_role": "pm", "payload": {"decision": "approved"}}, {"message_type": "task_assignment", "recipient_role": "developer", "payload": {"branch_name": "agent/test"}}]}'
    result = parse_agent_output(raw)
    assert len(result) == 2
    assert result[0]["message_type"] == "proposal_review"
    assert result[1]["message_type"] == "task_assignment"


def test_unified_format_empty_messages():
    raw = '{"schema_version": 1, "messages": []}'
    result = parse_agent_output(raw)
    assert result == []


def test_unified_format_fenced():
    raw = '```json\n{"schema_version": 1, "messages": [{"message_type": "review_result", "recipient_role": "developer", "payload": {"decision": "approved"}}]}\n```'
    result = parse_agent_output(raw)
    assert len(result) == 1


def test_unified_format_no_schema_version():
    """schema_version is optional — accepted when absent."""
    raw = '{"messages": [{"message_type": "proposal", "recipient_role": "architect", "payload": {"title": "X"}}]}'
    result = parse_agent_output(raw)
    assert len(result) == 1


def test_unified_format_wrong_schema_version():
    """Wrong schema_version is rejected (returns empty)."""
    raw = '{"schema_version": 99, "messages": [{"message_type": "proposal", "recipient_role": "architect", "payload": {"title": "X"}}]}'
    result = parse_agent_output(raw)
    assert result == []


# ── Validation ──

def test_validate_valid_message():
    assert _validate_message({"message_type": "proposal", "payload": {"title": "X"}}) is True


def test_validate_invalid_message_type():
    assert _validate_message({"message_type": "invalid_type", "payload": {}}) is False


def test_validate_missing_payload():
    assert _validate_message({"message_type": "proposal"}) is False


def test_validate_invalid_recipient_role():
    assert _validate_message({"message_type": "proposal", "recipient_role": "invalid", "payload": {}}) is False


def test_validate_null_recipient_role():
    assert _validate_message({"message_type": "task_progress", "recipient_role": None, "payload": {}}) is True


# ── Legacy conversion: PM ──

def test_legacy_pm_proposals():
    result = _convert_legacy({"proposals": [{"title": "A"}, {"title": "B"}]})
    assert len(result) == 2
    assert all(m["message_type"] == "proposal" for m in result)
    assert result[0]["payload"]["title"] == "A"


def test_legacy_pm_empty_proposals():
    result = _convert_legacy({"proposals": []})
    assert result == []


def test_legacy_pm_bare_array():
    result = _convert_legacy([{"title": "A"}, {"title": "B"}])
    assert len(result) == 2


def test_legacy_pm_single_proposal():
    result = _convert_legacy({"title": "Solo"})
    assert len(result) == 1
    assert result[0]["message_type"] == "proposal"


# ── Legacy conversion: Architect ──

def test_legacy_architect_approved():
    result = _convert_legacy({
        "decision": "approved",
        "concerns": [],
        "technical_spec": {"branch_name": "agent/test", "files_to_modify": ["a.py"]}
    })
    assert len(result) == 2
    assert result[0]["message_type"] == "proposal_review"
    assert result[0]["payload"]["decision"] == "approved"
    assert result[1]["message_type"] == "task_assignment"


def test_legacy_architect_rejected():
    result = _convert_legacy({"decision": "rejected", "concerns": ["too risky"]})
    assert len(result) == 1
    assert result[0]["message_type"] == "proposal_review"


# ── Legacy conversion: Developer ──

def test_legacy_developer_completed():
    result = _convert_legacy({
        "status": "completed",
        "branch_name": "agent/fix",
        "files_changed": ["a.py"],
        "tests_passed": True,
        "tests_added": ["test_a"],
        "notes": "tricky bit",
    })
    assert len(result) == 2
    assert result[0]["message_type"] == "task_progress"
    assert result[1]["message_type"] == "review_request"
    assert result[1]["payload"]["tests_added"] == ["test_a"]
    assert result[1]["payload"]["notes"] == "tricky bit"


def test_legacy_developer_blocked():
    result = _convert_legacy({"status": "blocked", "branch_name": "agent/fix"})
    assert len(result) == 1
    assert result[0]["message_type"] == "task_progress"


# ── Legacy conversion: Reviewer ──

def test_legacy_reviewer_with_summary():
    result = _convert_legacy({
        "decision": "changes_requested",
        "summary": "Needs fixes",
        "blocking_issues": ["bug on line 42"],
        "comments": [],
    })
    assert len(result) == 1
    assert result[0]["message_type"] == "review_result"
    assert result[0]["payload"]["decision"] == "changes_requested"


def test_legacy_reviewer_approved():
    result = _convert_legacy({
        "decision": "approved",
        "summary": "LGTM",
        "comments": [],
    })
    assert len(result) == 1
    assert result[0]["message_type"] == "review_result"


# ── JSON extraction ──

def test_extract_json_fenced():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_raw():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_embedded():
    assert _extract_json('Some text\n{"a": 1}\nMore text') == {"a": 1}


def test_extract_json_none():
    assert _extract_json("Just plain text with no JSON") is None


# ── Full parse with narrative ──

def test_parse_prose_returns_empty():
    result = parse_agent_output("Now let me explore the codebase...")
    assert result == []


def test_parse_narrative_with_embedded_unified():
    raw = 'Here is my analysis:\n{"schema_version": 1, "messages": [{"message_type": "proposal", "recipient_role": "architect", "payload": {"title": "Fix"}}]}\nDone.'
    result = parse_agent_output(raw)
    assert len(result) == 1
