"""Tests for configurable stale/stuck thresholds in MonitorUI and web API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agents.monitor import MonitorApp, MonitorUI
from agents.web import WebDashboard


# ---------------------------------------------------------------------------
# MonitorApp / MonitorUI threshold injection
# ---------------------------------------------------------------------------


def test_monitor_app_default_thresholds():
    """MonitorApp uses class-level defaults when no overrides given."""
    app = MonitorApp.__new__(MonitorApp)
    assert app.STALE_AGENT_THRESHOLD == 60
    assert app.STUCK_THREAD_THRESHOLD == 300


def test_monitor_app_custom_thresholds():
    """MonitorApp accepts custom thresholds that override class defaults."""
    app = MonitorApp("redis://localhost:6379/0",
                     stale_agent_threshold=120,
                     stuck_thread_threshold=600)
    assert app.STALE_AGENT_THRESHOLD == 120
    assert app.STUCK_THREAD_THRESHOLD == 600


def test_monitor_app_partial_override():
    """Only the provided threshold is overridden; the other keeps the default."""
    app = MonitorApp("redis://localhost:6379/0", stale_agent_threshold=90)
    assert app.STALE_AGENT_THRESHOLD == 90
    assert app.STUCK_THREAD_THRESHOLD == 300  # class default


def test_monitor_ui_passes_thresholds():
    """MonitorUI stores threshold params for forwarding to MonitorApp."""
    ui = MonitorUI("redis://localhost:6379/0",
                   stale_agent_threshold=45,
                   stuck_thread_threshold=900)
    assert ui.stale_agent_threshold == 45
    assert ui.stuck_thread_threshold == 900


def test_monitor_ui_defaults_to_none():
    """MonitorUI defaults to None (meaning MonitorApp class defaults apply)."""
    ui = MonitorUI("redis://localhost:6379/0")
    assert ui.stale_agent_threshold is None
    assert ui.stuck_thread_threshold is None


# ---------------------------------------------------------------------------
# Web API /api/exceptions — stale-agent payload includes threshold
# ---------------------------------------------------------------------------


@dataclass
class FakeAgent:
    agent_id: str
    status: str
    heartbeat: float | None
    heartbeat_age: float | None = None
    current_task: str | None = None


@dataclass
class FakeSnapshot:
    agents: list
    orchestrator_heartbeat: float | None = None
    streams: list = None
    metrics: dict = None

    def __post_init__(self):
        if self.streams is None:
            self.streams = []
        if self.metrics is None:
            self.metrics = {}


def _make_web_dashboard(idle_threshold: int = 600) -> WebDashboard:
    d = WebDashboard(redis_url="redis://localhost:6379/0", idle_threshold=idle_threshold)
    d.bus = AsyncMock()
    d.bus.redis = AsyncMock()
    d.bus.connect = AsyncMock()
    return d


def _build_exc_app(dashboard: WebDashboard) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/exceptions", dashboard._handle_exceptions)
    return app


@pytest.mark.asyncio
async def test_exceptions_stale_agent_includes_threshold():
    """Stale-agent entries in /api/exceptions must include the configured threshold."""
    dashboard = _make_web_dashboard(idle_threshold=900)

    now = time.time()
    stale_agent = FakeAgent(
        agent_id="developer-1",
        status="active",
        heartbeat=now - 1000,  # 1000s old > 900 threshold
    )
    snapshot = FakeSnapshot(agents=[stale_agent])

    with patch("agents.web.load_snapshot", new_callable=AsyncMock, return_value=snapshot):
        # Mock the thread model and gates to avoid Redis calls
        dashboard._build_thread_model = AsyncMock(return_value={})
        dashboard._get_gates_data = AsyncMock(return_value=[])
        dashboard.bus.redis.hgetall = AsyncMock(return_value={})

        async with TestClient(TestServer(_build_exc_app(dashboard))) as client:
            resp = await client.get("/api/exceptions")
            assert resp.status == 200
            body = await resp.json()

            assert len(body["stale_agents"]) == 1
            agent_entry = body["stale_agents"][0]
            assert agent_entry["agent_id"] == "developer-1"
            assert agent_entry["threshold"] == 900
            assert agent_entry["heartbeat_age"] >= 900


@pytest.mark.asyncio
async def test_exceptions_no_stale_below_threshold():
    """Agents with heartbeat age below threshold should not appear as stale."""
    dashboard = _make_web_dashboard(idle_threshold=600)

    now = time.time()
    healthy_agent = FakeAgent(
        agent_id="developer-1",
        status="active",
        heartbeat=now - 100,  # 100s old < 600 threshold
    )
    snapshot = FakeSnapshot(agents=[healthy_agent])

    with patch("agents.web.load_snapshot", new_callable=AsyncMock, return_value=snapshot):
        dashboard._build_thread_model = AsyncMock(return_value={})
        dashboard._get_gates_data = AsyncMock(return_value=[])
        dashboard.bus.redis.hgetall = AsyncMock(return_value={})

        async with TestClient(TestServer(_build_exc_app(dashboard))) as client:
            resp = await client.get("/api/exceptions")
            body = await resp.json()
            assert len(body["stale_agents"]) == 0
