"""Envelope and MessageType — the wire format for inter-agent communication."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Guards against a runaway message — NOT a budget for prose.
#
# MAX_STRING_FIELD used to be 500, applied to every string in every payload before
# publish. That is a second truncator, independent of the output schema, and it cut deeper
# than the schema did: measured review concerns run 886 to 1,107 characters, so removing
# the schema's limit alone would have moved the cut from 300 to 500 and left the text just
# as unreadable.
#
# Prose is no longer truncated per field. A complete message costs a few hundred tokens
# once; an incomplete one costs a reader guessing at the missing context while doing the
# work, plus a round to recover what was cut. The cheap option is to let it through.
#
# What remains is a TOTAL size guard, raised from 8 KB - which a single honest proposal
# with six prose fields could reach, and exceeding it DROPS THE MESSAGE - to a ceiling no
# real message approaches. The two shrink passes below are kept, because if something ever
# does run away, a shortened message still beats a discarded one.
MAX_PAYLOAD_BYTES = 262144  # 256 KB
MAX_STRING_FIELD = None     # no per-field truncation
MAX_ARRAY_ITEMS = 10        # count, not length: matches the schema's own maxItems
MAX_NESTING_DEPTH = 3


class MessageType(str, Enum):
    SYSTEM = "system"
    PROPOSAL = "proposal"
    CODEBASE_ANALYSIS = "codebase_analysis"
    DESIGN_FEEDBACK = "design_feedback"
    PROPOSAL_REVIEW = "proposal_review"
    TASK_ASSIGNMENT = "task_assignment"
    TASK_PROGRESS = "task_progress"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    USER_GATE = "user_gate"
    CLI_TRACE = "cli_trace"


class PayloadTooLarge(Exception):
    """Raised when a payload cannot be reduced below MAX_PAYLOAD_BYTES."""


def normalize_payload(payload: dict) -> dict:
    """Recursively truncate and cap payload fields to enforce size limits.

    Applied to all workflow messages before publish. Raises PayloadTooLarge
    if the payload cannot be reduced below MAX_PAYLOAD_BYTES.
    """
    normalized = _normalize_value(payload, depth=0)

    # Hard size enforcement — block if still too large
    serialized = json.dumps(normalized, ensure_ascii=False).encode()
    if len(serialized) > MAX_PAYLOAD_BYTES:
        # Aggressive pass: halve all strings
        normalized = _shrink_strings(normalized, max_len=200)
        serialized = json.dumps(normalized, ensure_ascii=False).encode()

    if len(serialized) > MAX_PAYLOAD_BYTES:
        # Final pass: cut to 100 chars
        normalized = _shrink_strings(normalized, max_len=100)
        serialized = json.dumps(normalized, ensure_ascii=False).encode()

    if len(serialized) > MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(
            f"Payload {len(serialized)} bytes exceeds {MAX_PAYLOAD_BYTES} after normalization"
        )

    return normalized


def _normalize_value(value, depth: int = 0):
    """Recursively normalize a value: truncate strings, cap arrays, recurse into dicts."""
    if depth > MAX_NESTING_DEPTH:
        return None

    if isinstance(value, str):
        if MAX_STRING_FIELD is None:
            return value
        return value[:MAX_STRING_FIELD] if len(value) > MAX_STRING_FIELD else value

    if isinstance(value, list):
        return [
            _normalize_value(item, depth + 1)
            for item in value[:MAX_ARRAY_ITEMS]
        ]

    if isinstance(value, dict):
        return {
            k: _normalize_value(v, depth + 1)
            for k, v in value.items()
        }

    # int, float, bool, None — pass through
    return value


def _shrink_strings(value, max_len: int):
    """Aggressively shrink all strings in a nested structure."""
    if isinstance(value, str):
        return value[:max_len] if len(value) > max_len else value
    if isinstance(value, list):
        return [_shrink_strings(item, max_len) for item in value]
    if isinstance(value, dict):
        return {k: _shrink_strings(v, max_len) for k, v in value.items()}
    return value


def payload_hash(thread_id: str, message_type: str, payload: dict) -> str:
    """Compute a stable hash for dedup. Ignores timestamps and IDs."""
    content = json.dumps(
        {"thread": thread_id, "type": message_type, "payload": payload},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class Envelope(BaseModel):
    """A single message flowing through Redis streams."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    sender_id: str
    sender_role: str
    message_type: MessageType
    payload: dict = Field(default_factory=dict)
    thread_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    recipient_role: str | None = None

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> Envelope:
        if isinstance(data, bytes):
            data = data.decode()
        return cls.model_validate_json(data)
