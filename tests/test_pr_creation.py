"""Focused tests for PR creation branch preflight behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.orchestrator import Orchestrator


class FakeRedis:
    def __init__(self):
        self.data: dict[str, dict[str, str]] = {}

    async def hset(self, key: str, field: str, value: str) -> None:
        self.data.setdefault(key, {})[field] = value


class FakeMetrics:
    def __init__(self):
        self.counts: list[str] = []

    async def increment(self, key: str) -> None:
        self.counts.append(key)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()

    async def communicate(self):
        return self._stdout, self._stderr


def make_orchestrator() -> Orchestrator:
    orch = Orchestrator()
    orch.config = SimpleNamespace(
        system=SimpleNamespace(working_dir=".", pr_max_retries=2, pr_retry_delay=0, gate_timeout=5),
        safety=SimpleNamespace(human_approval_required=[]),
    )
    async def _noop_publish(*a, **kw): return "0-0"
    orch.bus = SimpleNamespace(redis=FakeRedis(), publish=_noop_publish)
    orch._metrics = FakeMetrics()
    return orch


@pytest.mark.asyncio
async def test_create_pr_skips_when_local_branch_missing(monkeypatch):
    orch = make_orchestrator()

    async def fake_run(*args):
        if args[:3] == ("git", "show-ref", "--verify"):
            return FakeProc(returncode=1, stderr="fatal: not a valid ref")
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(orch, "_run_subprocess", fake_run)

    await orch._create_pr_with_retry("thread-1", "agent/missing", "Title", "Summary")

    status = orch.bus.redis.data[orch.PR_SET_KEY]["thread-1"]
    assert status.startswith("skipped|")
    assert "local branch missing" in status
    assert orch._metrics.counts == []


@pytest.mark.asyncio
async def test_create_pr_skips_when_branch_has_no_diff(monkeypatch):
    orch = make_orchestrator()

    async def fake_run(*args):
        if args[:3] == ("git", "show-ref", "--verify"):
            return FakeProc(returncode=0, stdout="ref ok")
        if args[:4] == ("git", "rev-list", "--count", "main..agent/no-diff"):
            return FakeProc(returncode=0, stdout="0")
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(orch, "_run_subprocess", fake_run)

    await orch._create_pr_with_retry("thread-2", "agent/no-diff", "Title", "Summary")

    status = orch.bus.redis.data[orch.PR_SET_KEY]["thread-2"]
    assert status.startswith("skipped|")
    assert "no diff vs main" in status
    assert orch._metrics.counts == []


@pytest.mark.asyncio
async def test_create_pr_pushes_branch_before_opening_pr(monkeypatch):
    orch = make_orchestrator()
    commands: list[tuple[str, ...]] = []

    async def fake_run(*args):
        commands.append(args)
        if args == ("git", "show-ref", "--verify", "refs/heads/agent/ready"):
            return FakeProc(returncode=0, stdout="local")
        if args == ("git", "rev-list", "--count", "main..agent/ready"):
            return FakeProc(returncode=0, stdout="2")
        if args == ("git", "show-ref", "--verify", "refs/remotes/origin/agent/ready"):
            remote_checks = sum(1 for cmd in commands if cmd == args)
            if remote_checks == 1:
                return FakeProc(returncode=1, stderr="missing remote")
            return FakeProc(returncode=0, stdout="remote")
        if args == ("git", "push", "-u", "origin", "agent/ready"):
            return FakeProc(returncode=0, stderr="branch set up to track")
        if args[:4] == ("gh", "pr", "create", "--base"):
            return FakeProc(returncode=0, stdout="https://github.com/example/repo/pull/123")
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(orch, "_run_subprocess", fake_run)

    await orch._create_pr_with_retry("thread-3", "agent/ready", "Title", "Summary")

    status = orch.bus.redis.data[orch.PR_SET_KEY]["thread-3"]
    assert "https://github.com/example/repo/pull/123" in status
    assert ("git", "push", "-u", "origin", "agent/ready") in commands
    assert any(cmd[:3] == ("gh", "pr", "create") for cmd in commands)
    assert orch._metrics.counts == ["prs:created"]
