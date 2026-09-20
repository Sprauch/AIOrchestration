"""Tests for output-level safety enforcement in AgentProcess."""

import asyncio

import pytest

from agents.core.message import Envelope, MessageType
from agents.core.safety import SafetyChecker, SafetyConfig, build_gate_context
from agents.approval_console import HumanApprovalConsole
from agents.roles.developer_agent import DeveloperAgent


class FakeCLI:
    async def send(self, prompt: str) -> str:
        return ""

    async def resume(self):
        pass


class FakeBus:
    def __init__(self):
        self.published = []
        self.redis = FakeRedis()
        self._wait_responses: dict[str, Envelope | None] = {}

    async def connect(self):
        pass

    async def close(self):
        pass

    async def publish(self, channel, envelope):
        self.published.append((channel, envelope))

    async def subscribe_simple(self, channels, last_ids=None):
        return
        yield

    async def get_history(self, channel, count=100):
        return []

    async def wait_for_message(self, channel, timeout_ms, last_id="$"):
        """Return a pre-configured response for the given channel, or None."""
        # Check if a response was pre-loaded for any gate on this channel
        for key, response in self._wait_responses.items():
            if key in channel:
                return response
        # No response pre-loaded — simulate timeout
        return None

    def preload_gate_response(self, gate_id_fragment: str, response: Envelope | None):
        """Pre-load a response that wait_for_message will return for a matching channel."""
        self._wait_responses[gate_id_fragment] = response


class FakeRedis:
    async def set(self, key, value, **kwargs):
        pass

    async def get(self, key):
        return None

    async def xlen(self, key):
        return 0


def _make_agent(safety=None):
    return DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(), bus=FakeBus(),
        safety=safety,
    )


def _make_safety():
    return SafetyChecker(SafetyConfig(
        blocked_patterns=["rm -rf", "DROP TABLE"],
        protected_files=[".env", "vercel.json"],
        branch_prefix="agent/",
        never_push_to=["main", "master"],
        human_approval_required=["large_change"],
        max_files_per_change=3,
    ))


@pytest.mark.asyncio
async def test_output_blocks_bad_branch():
    agent = _make_agent(safety=_make_safety())
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"branch_name": "feature/bad-branch", "status": "completed"},
    )
    assert await agent._check_output_safety(env) is False


@pytest.mark.asyncio
async def test_output_allows_good_branch():
    agent = _make_agent(safety=_make_safety())
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"branch_name": "agent/good-branch", "status": "completed"},
    )
    assert await agent._check_output_safety(env) is True


@pytest.mark.asyncio
async def test_output_blocks_protected_file():
    agent = _make_agent(safety=_make_safety())
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [".env", "app/main.py"]},
    )
    assert await agent._check_output_safety(env) is False


@pytest.mark.asyncio
async def test_output_allows_safe_files():
    agent = _make_agent(safety=_make_safety())
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": ["app/main.py", "tests/test_api.py"]},
    )
    assert await agent._check_output_safety(env) is True


@pytest.mark.asyncio
async def test_output_blocks_dangerous_text_in_approach():
    agent = _make_agent(safety=_make_safety())
    env = Envelope(
        sender_id="arch-1", sender_role="architect",
        message_type=MessageType.TASK_ASSIGNMENT,
        payload={
            "approach": "First rm -rf the old directory",
            "branch_name": "agent/cleanup",
        },
    )
    assert await agent._check_output_safety(env) is False


@pytest.mark.asyncio
async def test_output_passes_without_safety():
    agent = _make_agent(safety=None)
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.TASK_PROGRESS,
        payload={"branch_name": "anything/goes"},
    )
    assert await agent._check_output_safety(env) is True


@pytest.mark.asyncio
async def test_output_large_change_emits_gate_and_blocks_on_timeout():
    """Large changes emit a HUMAN_GATE and block via XREAD.
    With no response, wait_for_message returns None (timeout).
    """
    agent = _make_agent(safety=_make_safety())
    agent.gate_timeout = 1  # 1 second for test

    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [f"file{i}.py" for i in range(10)]},
    )
    result = await agent._check_output_safety(env)
    assert result is False

    # Verify the gate was published
    gates = [
        (ch, e) for ch, e in agent.bus.published
        if e.message_type == MessageType.HUMAN_GATE
    ]
    assert len(gates) == 1
    assert gates[0][1].payload["action"] == "large_change"


@pytest.mark.asyncio
async def test_output_large_change_approved():
    """Large change is approved when wait_for_message returns a grant."""
    safety = _make_safety()
    bus = FakeBus()
    agent = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(), bus=bus,
        safety=safety,
        gate_timeout=2,
    )

    # Pre-load an approval response for any gate channel
    bus.preload_gate_response("gate-responses:", Envelope(
        sender_id="human", sender_role="human",
        message_type=MessageType.SYSTEM,
        payload={"action": "approval_granted"},
    ))

    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [f"file{i}.py" for i in range(10)]},
    )
    result = await agent._check_output_safety(env)
    assert result is True


@pytest.mark.asyncio
async def test_output_large_change_denied():
    """Large change is denied when wait_for_message returns a denial."""
    safety = _make_safety()
    bus = FakeBus()
    agent = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(), bus=bus,
        safety=safety,
        gate_timeout=2,
    )

    bus.preload_gate_response("gate-responses:", Envelope(
        sender_id="human", sender_role="human",
        message_type=MessageType.SYSTEM,
        payload={"action": "approval_denied"},
    ))

    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [f"file{i}.py" for i in range(10)]},
    )
    result = await agent._check_output_safety(env)
    assert result is False


# --- Structured gate context tests ---


def test_build_gate_context_required_fields():
    """build_gate_context produces all required top-level fields."""
    ctx = build_gate_context(
        operation="large_change",
        branch="agent/test",
        file_count=5,
        safety_summary="escalated",
        escalation_reason="5 files in files_changed",
    )
    assert ctx["operation"] == "large_change"
    assert ctx["branch"] == "agent/test"
    assert ctx["file_count"] == 5
    assert ctx["safety_summary"] == "escalated"
    assert ctx["escalation_reason"] == "5 files in files_changed"


def test_build_gate_context_with_files():
    """build_gate_context includes classified file list."""
    files = [{"path": "a.py", "protected": False}, {"path": ".env", "protected": True}]
    ctx = build_gate_context(operation="protected_file", files=files)
    assert ctx["files"] == files
    assert ctx["operation"] == "protected_file"


def test_build_gate_context_shows_every_file():
    """The gate context lists EVERY file, however many there are.

    It used to stop at 20. A gate exists so a person can decide whether to allow a
    change, and hiding the 21st file from that person defeats the only purpose the
    context has — the more files a change touches, the more that decision depends on
    seeing all of them.
    """
    files = [{"path": f"f{i}.py", "protected": False} for i in range(30)]
    ctx = build_gate_context(operation="large_change", files=files)
    assert len(ctx["files"]) == 30
    assert ctx["files"][-1]["path"] == "f29.py"


def test_build_gate_context_extra_fields():
    """build_gate_context merges extra dict into context."""
    ctx = build_gate_context(operation="create_pr", extra={"pr_title": "Fix bug"})
    assert ctx["pr_title"] == "Fix bug"


def test_build_gate_context_omits_none_optional_fields():
    """build_gate_context omits branch/files/file_count when not provided."""
    ctx = build_gate_context(operation="test_op")
    assert "branch" not in ctx
    assert "files" not in ctx
    assert "file_count" not in ctx


def test_safety_checker_classify_files():
    """SafetyChecker.classify_files marks protected files correctly."""
    safety = _make_safety()
    result = safety.classify_files(["app.py", ".env", "tests/test.py"])
    assert result[0] == {"path": "app.py", "protected": False}
    assert result[1] == {"path": ".env", "protected": True}
    assert result[2] == {"path": "tests/test.py", "protected": False}


@pytest.mark.asyncio
async def test_large_change_gate_has_structured_context():
    """Large change gate emits structured context with required fields."""
    agent = _make_agent(safety=_make_safety())
    agent.gate_timeout = 1

    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [f"file{i}.py" for i in range(10)]},
    )
    await agent._check_output_safety(env)

    gates = [
        (ch, e) for ch, e in agent.bus.published
        if e.message_type == MessageType.HUMAN_GATE
    ]
    assert len(gates) == 1
    context = gates[0][1].payload["context"]
    assert isinstance(context, dict)
    assert context["operation"] == "large_change"
    assert context["file_count"] == 10
    assert context["safety_summary"] == "escalated"
    assert "files" in context
    assert len(context["files"]) == 10


@pytest.mark.asyncio
async def test_large_change_gate_files_are_classified():
    """Structured context includes file classification with protected flags."""
    agent = _make_agent(safety=_make_safety())
    agent.gate_timeout = 1

    files_safe = [f"file{i}.py" for i in range(10)]
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": files_safe},
    )
    await agent._check_output_safety(env)

    gates = [
        (ch, e) for ch, e in agent.bus.published
        if e.message_type == MessageType.HUMAN_GATE
    ]
    context = gates[0][1].payload["context"]
    for f_entry in context["files"]:
        assert "path" in f_entry
        assert "protected" in f_entry
        assert f_entry["protected"] is False


# --- Legacy/graceful context handling tests ---


def test_format_context_with_structured_dict():
    """approval_console renders structured context as multi-line."""
    ctx = build_gate_context(
        operation="large_change",
        branch="agent/fix",
        files=[{"path": "a.py", "protected": False}, {"path": ".env", "protected": True}],
        file_count=2,
        safety_summary="escalated",
        escalation_reason="2 files",
    )
    result = HumanApprovalConsole._format_context(ctx)
    assert "large_change" in result
    assert "agent/fix" in result
    assert "a.py" in result
    assert "[PROTECTED]" in result
    assert "escalated" in result


def test_format_context_with_legacy_string():
    """approval_console handles legacy plain-string context."""
    result = HumanApprovalConsole._format_context("file1.py, file2.py")
    assert result == "file1.py, file2.py"


def test_format_context_with_none():
    """approval_console handles None context gracefully."""
    assert HumanApprovalConsole._format_context(None) == ""


def test_format_context_with_empty_string():
    """approval_console handles empty string context gracefully."""
    assert HumanApprovalConsole._format_context("") == ""


def test_format_context_with_empty_dict():
    """approval_console handles empty dict context gracefully."""
    result = HumanApprovalConsole._format_context({})
    assert result == ""


# --- Gate timeout system event tests ---


@pytest.mark.asyncio
async def test_gate_timeout_emits_system_event():
    """Gate timeout publishes a system event with gate_id, action, agent, and resolution_at."""
    agent = _make_agent(safety=_make_safety())
    agent.gate_timeout = 1  # 1 second for fast timeout

    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [f"file{i}.py" for i in range(10)]},
    )
    result = await agent._check_output_safety(env)
    assert result is False

    # Find the system event for the timeout
    system_events = [
        (ch, e) for ch, e in agent.bus.published
        if e.message_type == MessageType.SYSTEM and e.payload.get("action") == "gate_timeout"
    ]
    assert len(system_events) == 1
    ch, evt = system_events[0]
    assert ch == "system"
    assert evt.payload["action"] == "gate_timeout"
    assert "gate_id" in evt.payload
    assert evt.payload["agent"] == "dev-1"
    assert "resolution_at" in evt.payload
    # gate_id should match the HUMAN_GATE that was emitted
    gates = [
        e for _, e in agent.bus.published
        if e.message_type == MessageType.HUMAN_GATE
    ]
    assert len(gates) == 1
    assert evt.payload["gate_id"] == gates[0].id


# ── assign_task gate: stop the work before it is paid for ────────────────


def _gating_safety(actions):
    return SafetyChecker(SafetyConfig(
        branch_prefix="agent/",
        never_push_to=["main", "master"],
        human_approval_required=actions,
        max_files_per_change=50,
    ))


def _task_assignment():
    return Envelope(
        sender_id="architect-1", sender_role="architect",
        message_type=MessageType.TASK_ASSIGNMENT,
        payload={
            "branch_name": "agent/some-task",
            "approach": "First line of the approach\nsecond line",
            "files_to_modify": ["src/a.ts", "src/b.ts"],
            "files_to_create": [],
            "acceptance_criteria": ["it works"],
            "testing_strategy": "unit tests",
        },
    )


@pytest.mark.asyncio
async def test_task_assignment_is_gated_when_configured():
    """A denied assignment dispatches no work at all.

    Gating only create_pr meant the first human decision came AFTER a developer had
    worked a full cycle in a worktree and a reviewer had read the result. If the premise
    was wrong, the tokens were already spent. This gate is the point where saying no is
    still free.
    """
    agent = _make_agent(safety=_gating_safety(["assign_task"]))
    asked = {}

    async def deny(action, reason, context, thread_id):
        asked.update(action=action, reason=reason, context=context)
        return False

    agent._request_human_approval = deny
    assert await agent._check_output_safety(_task_assignment()) is False
    assert asked["action"] == "assign_task"
    # The reason is the approach's first line, not the whole thing
    assert asked["reason"] == "First line of the approach"
    # The approver sees what the work actually is
    assert asked["context"]["approach"].startswith("First line")
    assert asked["context"]["acceptance_criteria"] == ["it works"]
    assert asked["context"]["file_count"] == 2


@pytest.mark.asyncio
async def test_approved_task_assignment_proceeds():
    agent = _make_agent(safety=_gating_safety(["assign_task"]))

    async def approve(action, reason, context, thread_id):
        return True

    agent._request_human_approval = approve
    assert await agent._check_output_safety(_task_assignment()) is True


@pytest.mark.asyncio
async def test_task_assignment_not_gated_when_not_configured():
    """Leaving assign_task out of the list keeps the old behaviour exactly."""
    agent = _make_agent(safety=_gating_safety(["create_pr"]))
    called = False

    async def should_not_run(action, reason, context, thread_id):
        nonlocal called
        called = True
        return False

    agent._request_human_approval = should_not_run
    assert await agent._check_output_safety(_task_assignment()) is True
    assert called is False
