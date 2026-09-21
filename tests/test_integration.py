"""Integration tests — exercise multi-component interactions with fakes."""

import asyncio
import json

import pytest

from agents.core.message import Envelope, MessageType
from agents.core.safety import SafetyChecker, SafetyConfig
from agents.roles.pm_agent import PMAgent
from agents.roles.architect_agent import ArchitectAgent
from agents.roles.developer_agent import DeveloperAgent
from agents.roles.reviewer_agent import ReviewerAgent


# ── Fakes ──────────────────────────────────────────────────

class FakeCLI:
    """Fake CLI that returns a canned response."""

    def __init__(self, response: str = ""):
        self._response = response

    async def send(self, prompt: str) -> str:
        return self._response

    async def resume(self):
        pass


class FakeRedis:
    def __init__(self):
        self._hdata: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}

    async def set(self, key, value):
        pass

    async def get(self, key):
        return None

    async def xlen(self, key):
        return 0

    async def scard(self, key):
        return len(self._sets.get(key, set()))

    async def sadd(self, key, *members):
        self._sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        s = self._sets.get(key, set())
        for m in members:
            s.discard(m)

    async def sismember(self, key, member):
        return member in self._sets.get(key, set())

    async def hget(self, key, field):
        return self._hdata.get(key, {}).get(field)

    async def hset(self, key, field, value=None):
        self._hdata.setdefault(key, {})[field] = value

    async def hdel(self, key, field):
        self._hdata.get(key, {}).pop(field, None)

    async def hlen(self, key):
        return len(self._hdata.get(key, {}))

    async def hsetnx(self, key, field, value):
        self._hdata.setdefault(key, {})
        if field not in self._hdata[key]:
            self._hdata[key][field] = value
            return 1
        return 0

    async def hincrby(self, key, field, amount):
        self._hdata.setdefault(key, {})
        current = int(self._hdata[key].get(field, 0))
        self._hdata[key][field] = str(current + amount)

    async def eval(self, script, num_keys, *args):
        """Simulate ThreadGuard Lua script."""
        key, field = args[0], args[1]
        max_rounds, incr = int(args[2]), int(args[3])
        current = int(self._hdata.get(key, {}).get(field, 0))
        if current >= max_rounds:
            return -1
        if incr == 1:
            await self.hincrby(key, field, 1)
        return current


class FakeBus:
    """In-memory message bus that records published messages."""

    def __init__(self):
        self.published: list[tuple[str, Envelope]] = []
        self.redis = FakeRedis()
        self._queue: asyncio.Queue[Envelope] = asyncio.Queue()
        self._responses: dict[str, asyncio.Queue] = {}

    async def connect(self):
        pass

    async def close(self):
        pass

    async def publish(self, channel: str, envelope: Envelope) -> str:
        self.published.append((channel, envelope))
        return "fake-id"

    async def subscribe_simple(self, channels, last_ids=None):
        while True:
            env = await self._queue.get()
            yield env

    async def get_history(self, channel, count=100):
        return []

    async def wait_for_message(
        self, channel: str, timeout_ms: int, last_id: str = "$",
    ) -> Envelope | None:
        """Block for one message on `channel`, or return None when the wait expires.

        The double had drifted from MessageBus, which grew this method for the user
        approval gate: base_agent blocks on a per-gate response channel, so without it
        every gated action raised AttributeError instead of waiting. Mirrors the real
        contract — a reply is returned, an expired wait returns None rather than raising.
        """
        queue = self._responses.setdefault(channel, asyncio.Queue())
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            return None

    def inject(self, envelope: Envelope):
        """Push a message into the subscription queue."""
        self._queue.put_nowait(envelope)

    def respond(self, channel: str, envelope: Envelope):
        """Answer a waiter blocked on `channel` — the approval a user would give."""
        self._responses.setdefault(channel, asyncio.Queue()).put_nowait(envelope)


# ── Tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pm_processes_trigger_and_publishes_proposals():
    """PM receives a startup trigger and publishes structured proposals."""
    response = json.dumps({
        "proposals": [
            {"title": "Add input validation", "priority": 2, "category": "security"},
            {"title": "Fix N+1 query", "priority": 1, "category": "performance"},
        ]
    })
    response = f"```json\n{response}\n```"

    bus = FakeBus()
    agent = PMAgent(
        agent_id="pm-1", role="pm",
        cli_session=FakeCLI(response), bus=bus,
    )

    trigger = Envelope(
        sender_id="orchestrator", sender_role="system",
        message_type=MessageType.SYSTEM,
        payload={"action": "trigger_analysis"},
    )
    bus.inject(trigger)

    # Run agent briefly, then stop
    task = asyncio.create_task(agent.start())
    await asyncio.sleep(0.3)
    await agent.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have published 2 proposals + traces
    proposals = [(ch, env) for ch, env in bus.published if env.message_type == MessageType.PROPOSAL]
    assert len(proposals) == 2
    assert proposals[0][1].payload["title"] == "Add input validation"
    assert proposals[1][1].payload["title"] == "Fix N+1 query"


@pytest.mark.asyncio
async def test_architect_approves_and_creates_task():
    """Architect receives a proposal, approves it, publishes review + task."""
    response = json.dumps({
        "decision": "approved",
        "reasoning": "Good proposal",
        "technical_spec": {
            "approach": "Add validators",
            "files_to_modify": ["app/api.py"],
            "acceptance_criteria": ["Validation works"],
            "branch_name": "agent/add-validation",
        }
    })
    response = f"```json\n{response}\n```"

    bus = FakeBus()
    agent = ArchitectAgent(
        agent_id="arch-1", role="architect",
        cli_session=FakeCLI(response), bus=bus,
    )

    proposal = Envelope(
        sender_id="pm-1", sender_role="pm",
        message_type=MessageType.PROPOSAL,
        payload={"title": "Add validation", "priority": 2},
    )
    bus.inject(proposal)

    task = asyncio.create_task(agent.start())
    await asyncio.sleep(0.3)
    await agent.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    reviews = [env for ch, env in bus.published if env.message_type == MessageType.PROPOSAL_REVIEW]
    tasks = [env for ch, env in bus.published if env.message_type == MessageType.TASK_ASSIGNMENT]
    assert len(reviews) == 1
    assert reviews[0].payload["decision"] == "approved"
    assert len(tasks) == 1
    assert tasks[0].payload["branch_name"] == "agent/add-validation"


@pytest.mark.asyncio
async def test_developer_completes_and_submits_review():
    """Developer implements a task and submits for review."""
    response = json.dumps({
        "status": "completed",
        "branch_name": "agent/fix-tests",
        "files_changed": ["app/api.py", "tests/test_api.py"],
        "changes_summary": "Added input validation",
        "tests_added": ["test_validate_input"],
        "tests_passed": True,
        "notes": "All good",
    })
    response = f"```json\n{response}\n```"

    bus = FakeBus()
    agent = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(response), bus=bus,
    )

    task_env = Envelope(
        sender_id="arch-1", sender_role="architect",
        message_type=MessageType.TASK_ASSIGNMENT,
        payload={"branch_name": "agent/fix-tests", "files_to_modify": ["app/api.py"]},
    )
    bus.inject(task_env)

    run_task = asyncio.create_task(agent.start())
    await asyncio.sleep(0.3)
    await agent.stop()
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass

    progress = [env for ch, env in bus.published if env.message_type == MessageType.TASK_PROGRESS]
    review_reqs = [env for ch, env in bus.published if env.message_type == MessageType.REVIEW_REQUEST]
    assert len(progress) == 1
    assert progress[0].payload["status"] == "completed"
    assert len(review_reqs) == 1
    assert review_reqs[0].payload["branch_name"] == "agent/fix-tests"


@pytest.mark.asyncio
async def test_full_pipeline_cycle_with_fakes():
    """End-to-end: trigger -> PM -> Architect -> Developer -> Reviewer."""
    # Set up 4 agents with canned responses
    pm_response = '```json\n{"proposals": [{"title": "Improve logging", "priority": 3}]}\n```'
    arch_response = '```json\n{"decision": "approved", "reasoning": "OK", "technical_spec": {"approach": "Add logs", "files_to_modify": ["app/main.py"], "acceptance_criteria": ["Logs work"], "branch_name": "agent/logging"}}\n```'
    dev_response = '```json\n{"status": "completed", "branch_name": "agent/logging", "files_changed": ["app/main.py"], "changes_summary": "Added logging", "tests_passed": true}\n```'
    rev_response = '```json\n{"decision": "approved", "summary": "LGTM"}\n```'

    bus = FakeBus()

    pm = PMAgent(agent_id="pm-1", role="pm", cli_session=FakeCLI(pm_response), bus=bus)
    arch = ArchitectAgent(agent_id="arch-1", role="architect", cli_session=FakeCLI(arch_response), bus=bus)
    dev = DeveloperAgent(agent_id="dev-1", role="developer", cli_session=FakeCLI(dev_response), bus=bus)
    rev = ReviewerAgent(agent_id="rev-1", role="reviewer", cli_session=FakeCLI(rev_response), bus=bus)

    # Trigger PM
    bus.inject(Envelope(
        sender_id="orchestrator", sender_role="system",
        message_type=MessageType.SYSTEM,
        payload={"action": "trigger_analysis"},
    ))

    # Run PM
    pm_task = asyncio.create_task(pm.start())
    await asyncio.sleep(0.3)
    await pm.stop()
    pm_task.cancel()
    try:
        await pm_task
    except asyncio.CancelledError:
        pass

    # Feed PM's proposal to Architect
    proposals = [env for ch, env in bus.published if env.message_type == MessageType.PROPOSAL]
    assert len(proposals) >= 1
    bus.inject(proposals[0])

    arch_task = asyncio.create_task(arch.start())
    await asyncio.sleep(0.3)
    await arch.stop()
    arch_task.cancel()
    try:
        await arch_task
    except asyncio.CancelledError:
        pass

    # Feed Architect's task to Developer
    tasks = [env for ch, env in bus.published if env.message_type == MessageType.TASK_ASSIGNMENT]
    assert len(tasks) >= 1
    bus.inject(tasks[0])

    dev_task = asyncio.create_task(dev.start())
    await asyncio.sleep(0.3)
    await dev.stop()
    dev_task.cancel()
    try:
        await dev_task
    except asyncio.CancelledError:
        pass

    # Feed Developer's review request to Reviewer
    review_reqs = [env for ch, env in bus.published if env.message_type == MessageType.REVIEW_REQUEST]
    assert len(review_reqs) >= 1
    bus.inject(review_reqs[0])

    rev_task = asyncio.create_task(rev.start())
    await asyncio.sleep(0.3)
    await rev.stop()
    rev_task.cancel()
    try:
        await rev_task
    except asyncio.CancelledError:
        pass

    # Verify full pipeline completed
    review_results = [env for ch, env in bus.published if env.message_type == MessageType.REVIEW_RESULT]
    assert len(review_results) >= 1
    assert review_results[0].payload["decision"] == "approved"


@pytest.mark.asyncio
async def test_output_safety_blocks_bad_branch_in_pipeline():
    """Developer output with a bad branch name is blocked by output safety."""
    dev_response = '```json\n{"status": "completed", "branch_name": "main", "files_changed": ["app/api.py"], "changes_summary": "oops"}\n```'

    safety = SafetyChecker(SafetyConfig(
        branch_prefix="agent/",
        protected_files=[".env"],
    ))
    bus = FakeBus()
    dev = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(dev_response), bus=bus,
        safety=safety,
    )

    bus.inject(Envelope(
        sender_id="arch-1", sender_role="architect",
        message_type=MessageType.TASK_ASSIGNMENT,
        payload={"branch_name": "agent/test", "files_to_modify": ["app/api.py"]},
    ))

    task = asyncio.create_task(dev.start())
    await asyncio.sleep(0.3)
    await dev.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Both progress and review_request should be blocked (branch = "main")
    progress = [env for ch, env in bus.published if env.message_type == MessageType.TASK_PROGRESS]
    review_reqs = [env for ch, env in bus.published if env.message_type == MessageType.REVIEW_REQUEST]
    assert len(progress) == 0
    assert len(review_reqs) == 0


@pytest.mark.asyncio
async def test_gate_lifecycle_create_timeout_resolved():
    """Full gate lifecycle: gate created -> timeout -> system event with resolution."""
    safety = SafetyChecker(SafetyConfig(
        branch_prefix="agent/",
        protected_files=[".env"],
        user_approval_required=["large_change"],
        max_files_per_change=3,
    ))
    bus = FakeBus()
    dev = DeveloperAgent(
        agent_id="dev-1", role="developer",
        cli_session=FakeCLI(""), bus=bus,
        safety=safety,
        gate_timeout=1,
    )

    # Trigger a large change that requires user approval
    env = Envelope(
        sender_id="dev-1", sender_role="developer",
        message_type=MessageType.REVIEW_REQUEST,
        payload={"files_changed": [f"file{i}.py" for i in range(10)]},
    )
    result = await dev._check_output_safety(env)
    assert result is False  # Timed out, so blocked

    # Verify: 1) USER_GATE was created
    gates = [
        (ch, e) for ch, e in bus.published
        if e.message_type == MessageType.USER_GATE
    ]
    assert len(gates) == 1
    gate_id = gates[0][1].id
    gate_action = gates[0][1].payload["action"]
    assert gate_action == "large_change"

    # Verify: 2) Timeout system event was published
    timeout_events = [
        (ch, e) for ch, e in bus.published
        if e.message_type == MessageType.SYSTEM and e.payload.get("action") == "gate_timeout"
    ]
    assert len(timeout_events) == 1
    timeout_evt = timeout_events[0][1]
    assert timeout_evt.payload["gate_id"] == gate_id
    assert timeout_evt.payload["agent"] == "dev-1"
    assert "resolution_at" in timeout_evt.payload

    # Verify: 3) Deterministic precedence — first event wins
    # Publish a second (duplicate) resolution event for the same gate
    from agents.core.message import Envelope as Env
    dup_env = Env(
        sender_id="user", sender_role="user",
        message_type=MessageType.SYSTEM,
        payload={"action": "approval_granted", "gate_id": gate_id},
    )
    await bus.publish("system", dup_env)

    # Collect all resolution events for this gate_id
    all_resolutions = [
        e for _, e in bus.published
        if e.message_type == MessageType.SYSTEM and e.payload.get("gate_id") == gate_id
    ]
    # The first resolution is gate_timeout, second is approval_granted
    assert all_resolutions[0].payload["action"] == "gate_timeout"
    assert all_resolutions[1].payload["action"] == "approval_granted"
    # First-event-wins: the gate's outcome should be timed_out (enforced in web layer)
