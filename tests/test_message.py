"""Tests for Envelope, MessageType, and payload normalization."""

from agents.core.message import (
    Envelope, MessageType, normalize_payload,
    MAX_STRING_FIELD, MAX_ARRAY_ITEMS,
)


def test_envelope_roundtrip():
    env = Envelope(
        sender_id="pm-1",
        sender_role="pm",
        message_type=MessageType.PROPOSAL,
        payload={"title": "Fix tests", "priority": 2},
        thread_id="abc123",
    )
    json_str = env.to_json()
    restored = Envelope.from_json(json_str)

    assert restored.sender_id == "pm-1"
    assert restored.sender_role == "pm"
    assert restored.message_type == MessageType.PROPOSAL
    assert restored.payload["title"] == "Fix tests"
    assert restored.payload["priority"] == 2
    assert restored.thread_id == "abc123"


def test_envelope_from_bytes():
    env = Envelope(
        sender_id="dev-1",
        sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"status": "completed"},
    )
    data = env.to_json().encode()
    restored = Envelope.from_json(data)
    assert restored.sender_id == "dev-1"
    assert restored.payload["status"] == "completed"


def test_envelope_defaults():
    env = Envelope(
        sender_id="test",
        sender_role="system",
        message_type=MessageType.SYSTEM,
    )
    assert env.id  # auto-generated
    assert env.thread_id  # auto-generated
    assert env.timestamp  # auto-generated
    assert env.payload == {}
    assert env.recipient_role is None


def test_message_type_values():
    assert MessageType.PROPOSAL.value == "proposal"
    assert MessageType.REVIEW_RESULT.value == "review_result"
    assert MessageType.HUMAN_GATE.value == "human_gate"
    assert MessageType.CLI_TRACE.value == "cli_trace"


# ── Payload normalization ──────────────────────────────────

def test_normalize_truncates_long_strings():
    payload = {"reasoning": "x" * 1000}
    result = normalize_payload(payload)
    assert len(result["reasoning"]) == MAX_STRING_FIELD


def test_normalize_caps_arrays():
    payload = {"files": [f"file{i}.py" for i in range(20)]}
    result = normalize_payload(payload)
    assert len(result["files"]) == MAX_ARRAY_ITEMS


def test_normalize_preserves_short_fields():
    payload = {"title": "Fix bug", "priority": 2}
    result = normalize_payload(payload)
    assert result["title"] == "Fix bug"
    assert result["priority"] == 2


def test_normalize_truncates_strings_in_arrays():
    payload = {"issues": ["x" * 1000, "short"]}
    result = normalize_payload(payload)
    assert len(result["issues"][0]) == MAX_STRING_FIELD
    assert result["issues"][1] == "short"


def test_normalize_empty_payload():
    assert normalize_payload({}) == {}
