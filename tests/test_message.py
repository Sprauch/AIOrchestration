"""Tests for Envelope, MessageType, and payload normalization."""

import json

from agents.core.message import (
    Envelope, MessageType, normalize_payload,
    MAX_ARRAY_ITEMS, MAX_PAYLOAD_BYTES,
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

def test_normalize_leaves_long_prose_intact():
    """Prose is NOT truncated per field.

    This used to cut every string to 500 characters before publish — a second truncator,
    independent of the output schema, and the deeper of the two: measured review concerns
    run 886 to 1,107 characters, so it took the end off every real review. A complete
    message costs a few hundred tokens once; an incomplete one costs a reader guessing at
    the missing context, and usually a round to recover what was cut.
    """
    payload = {"reasoning": "x" * 1000}
    result = normalize_payload(payload)
    assert result["reasoning"] == payload["reasoning"]


def test_normalize_caps_arrays():
    payload = {"files": [f"file{i}.py" for i in range(20)]}
    result = normalize_payload(payload)
    assert len(result["files"]) == MAX_ARRAY_ITEMS


def test_normalize_preserves_short_fields():
    payload = {"title": "Fix bug", "priority": 2}
    result = normalize_payload(payload)
    assert result["title"] == "Fix bug"
    assert result["priority"] == 2


def test_normalize_leaves_prose_in_arrays_intact():
    """The same applies inside arrays — this is where concerns and comments live."""
    payload = {"issues": ["x" * 1000, "short"]}
    result = normalize_payload(payload)
    assert result["issues"][0] == payload["issues"][0]
    assert result["issues"][1] == "short"


def test_normalize_blocks_only_a_genuinely_runaway_payload():
    """The remaining guard is TOTAL size, and it is far above any real message.

    A single honest proposal - six prose fields plus file lists - could reach the old
    8 KB ceiling, and exceeding it DROPS the message rather than shortening it. The
    shrink passes are kept for the runaway case, because a shortened message still beats
    a discarded one.
    """
    realistic = {f"field_{i}": "x" * 1200 for i in range(8)}
    assert normalize_payload(realistic)  # nowhere near the ceiling

    runaway = {f"field_{i}": "x" * 100_000 for i in range(20)}
    shrunk = normalize_payload(runaway)  # shortened rather than dropped
    assert len(json.dumps(shrunk).encode()) <= MAX_PAYLOAD_BYTES


def test_normalize_empty_payload():
    assert normalize_payload({}) == {}
