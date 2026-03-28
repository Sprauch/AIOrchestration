"""Tests for Bearer-token auth on gate approve/deny endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agents.core.message import Envelope, MessageType
from agents.web import WebDashboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GATE_ID = "gate-abc-123"
TOKEN = "s3cret-token"


def _make_gate_envelope(gate_id: str = GATE_ID) -> Envelope:
    return Envelope(
        sender_id="developer-1",
        sender_role="developer",
        message_type=MessageType.HUMAN_GATE,
        payload={"action": "push_branch", "reason": "needs approval"},
        thread_id="thread-001",
        id=gate_id,
    )


def _fake_redis(gate_envelope: Envelope | None = None):
    """Return a mock Redis object that serves a single gate entry."""
    redis = AsyncMock()

    async def _xrange(key, *a, **kw):
        if "human-gates" in key and gate_envelope is not None:
            return [("1-0", {"data": gate_envelope.to_json()})]
        return []

    async def _xadd(key, fields, **kw):
        return "1-1"

    redis.xrange = _xrange
    redis.xadd = _xadd
    return redis


def _build_app(dashboard: WebDashboard) -> web.Application:
    """Build the aiohttp app with the same routes as WebDashboard.start()."""
    app = web.Application()
    app.router.add_get("/api/gates", dashboard._handle_gates)
    app.router.add_post("/api/gates/{gate_id}/approve", dashboard._handle_gate_approve)
    app.router.add_post("/api/gates/{gate_id}/deny", dashboard._handle_gate_deny)
    return app


def _make_dashboard(token: str | None) -> WebDashboard:
    d = WebDashboard(redis_url="redis://localhost:6379/0", gate_token=token)
    gate_env = _make_gate_envelope()
    d.bus = AsyncMock()
    d.bus.redis = _fake_redis(gate_env)
    d.bus.publish = AsyncMock()
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ── 401: missing token ────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_no_header_returns_401():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(f"/api/gates/{GATE_ID}/approve")
        assert resp.status == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
        body = await resp.json()
        assert "missing" in body["error"]


@pytest.mark.asyncio
async def test_deny_no_header_returns_401():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(f"/api/gates/{GATE_ID}/deny")
        assert resp.status == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


# ── 401: wrong token ─────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_wrong_token_returns_401():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(
            f"/api/gates/{GATE_ID}/approve",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401
        body = await resp.json()
        assert "invalid" in body["error"]


@pytest.mark.asyncio
async def test_approve_malformed_auth_returns_401():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(
            f"/api/gates/{GATE_ID}/approve",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status == 401


# ── 200: correct token ───────────────────────────────────


@pytest.mark.asyncio
async def test_approve_correct_token_returns_200():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(
            f"/api/gates/{GATE_ID}/approve",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["action"] == "approval_granted"
        assert body["gate_id"] == GATE_ID
        # Verify publish was called (gate-responses + system)
        assert dashboard.bus.publish.call_count == 2


@pytest.mark.asyncio
async def test_deny_correct_token_returns_200():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(
            f"/api/gates/{GATE_ID}/deny",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["action"] == "approval_denied"


# ── 404: valid token, unknown gate_id ─────────────────────


@pytest.mark.asyncio
async def test_approve_unknown_gate_returns_404():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(
            "/api/gates/nonexistent-gate/approve",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"]


# ── GET endpoints remain open (no auth required) ─────────


@pytest.mark.asyncio
async def test_get_gates_no_auth_required():
    dashboard = _make_dashboard(TOKEN)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.get("/api/gates")
        assert resp.status == 200


# ── No token configured: gate endpoints open ─────────────


@pytest.mark.asyncio
async def test_approve_without_token_config_returns_200():
    dashboard = _make_dashboard(None)
    async with TestClient(TestServer(_build_app(dashboard))) as client:
        resp = await client.post(f"/api/gates/{GATE_ID}/approve")
        assert resp.status == 200
        body = await resp.json()
        assert body["action"] == "approval_granted"
