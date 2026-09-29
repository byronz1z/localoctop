"""Tests for the bridge WS endpoint: token auth (query + header), register
frame handling, response routing, ping control frames, idle timeout, and
teardown/unregister on disconnect.

Token model (任务书 v1.1 §七): the client WSS authenticates with a
*bridge_token* — the mcp_token (Octop connector Bearer) is a different
secret and must be rejected here.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import WebSocketDisconnect

from mcp_localfs.bridge_ws import BridgeWSEndpoint
from mcp_localfs.config import Settings
from mcp_localfs.sessions import SessionRegistry
from mcp_localfs.tokens import KIND_BRIDGE

from conftest import FakeWebSocket


def _endpoint(settings: Settings, registry: SessionRegistry) -> BridgeWSEndpoint:
    return BridgeWSEndpoint(settings, registry)


REGISTER = {
    "type": "register",
    "token": "btok-alice",
    "client_id": "c-123",
    "hostname": "workstation-1",
    "allowed_dirs": ["/home/alice/Documents/Work"],
    "write_enabled": False,
    "version": "1.0.0",
}


@pytest.mark.asyncio
async def test_register_success(settings, registry):
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed(REGISTER)
    ws.feed_disconnect()
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    # Session should have been registered, then removed on disconnect.
    assert registry.get("alice") is None
    # The disconnect path must not crash and close code should be unset/1000.
    assert ws.closed_code in (None, 1000)


@pytest.mark.asyncio
async def test_token_via_authorization_header(settings, registry):
    ws = FakeWebSocket(headers={"authorization": "Bearer btok-bob"})
    ws.feed({**REGISTER, "token": "btok-bob", "client_id": "b-1"})
    ws.feed_disconnect()
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    # No exception; bob registered and unregistered cleanly.


@pytest.mark.asyncio
async def test_bad_token_rejected_before_accept(settings, registry):
    ws = FakeWebSocket(query={"token": "wrong"})
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    assert ws.closed_code == 1008  # policy violation
    assert registry.get("alice") is None
    assert registry.get("bob") is None


@pytest.mark.asyncio
async def test_missing_token_rejected(settings, registry):
    ws = FakeWebSocket()
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    assert ws.closed_code == 1008


@pytest.mark.asyncio
async def test_mcp_token_rejected_on_bridge_ws(settings, registry):
    """任务书 v1.1 §七 分向拒绝 2/2：a valid *mcp* token (Octop connector
    Bearer) must not authenticate the client WSS — the two token kinds are
    separate namespaces."""
    # tok-alice is alice's valid MCP token; on the bridge it is unknown.
    ws = FakeWebSocket(query={"token": "tok-alice"})
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    assert ws.closed_code == 1008
    assert registry.get("alice") is None


@pytest.mark.asyncio
async def test_revoked_bridge_token_rejected(settings, registry):
    """Bridge tokens can be revoked independently of the MCP side."""
    settings.token_store.revoke_user(KIND_BRIDGE, "alice")
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    assert ws.closed_code == 1008
    assert registry.get("alice") is None


@pytest.mark.asyncio
async def test_register_token_mismatch_rejected(settings, registry):
    """Handshake token says alice; register frame claims bob's token."""
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed({**REGISTER, "token": "btok-bob"})
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    assert ws.closed_code == 4401
    assert registry.get("alice") is None


@pytest.mark.asyncio
async def test_first_frame_must_be_register(settings, registry):
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed({"id": 1, "method": "read_file", "params": {}})  # not a register
    ep = _endpoint(settings, registry)
    await ep.handle(ws)
    assert ws.closed_code == 4403
    assert registry.get("alice") is None


@pytest.mark.asyncio
async def test_response_routed_to_pending_caller(settings, registry):
    """A bridge response frame resolves the registry future for that id."""
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed(REGISTER)
    ep = _endpoint(settings, registry)
    handle_task = asyncio.create_task(ep.handle(ws))

    # Wait for registration.
    for _ in range(100):
        if registry.get("alice"):
            break
        await asyncio.sleep(0.01)
    session = registry.get("alice")
    assert session is not None

    # Kick off a call; answer it through the fake socket.
    call_task = asyncio.create_task(registry.call("alice", "read_file", {"path": "a.txt"}))
    for _ in range(100):
        if ws.outbox:
            break
        await asyncio.sleep(0.01)
    sent = ws.outbox[0]
    ws.feed({"id": sent["id"], "result": {"path": "a.txt", "text": "ok", "encoding": "utf-8", "size": 2}})

    result = await asyncio.wait_for(call_task, timeout=2)
    assert result["text"] == "ok"

    ws.feed_disconnect()
    await asyncio.wait_for(handle_task, timeout=2)
    assert registry.get("alice") is None


@pytest.mark.asyncio
async def test_ping_control_frame_answered(settings, registry):
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed(REGISTER)
    ws.feed({"type": "ping", "id": 99})
    ws.feed_disconnect()
    ep = _endpoint(settings, registry)
    await asyncio.wait_for(ep.handle(ws), timeout=2)
    pongs = [m for m in ws.outbox if isinstance(m, dict) and m.get("result", {}).get("type") == "pong"]
    assert pongs, f"no pong in outbox: {ws.outbox}"


@pytest.mark.asyncio
async def test_reregister_frame_updates_metadata(settings, registry):
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed(REGISTER)
    ws.feed({"type": "register", "client_id": "c-999", "allowed_dirs": ["/x"], "write_enabled": True})
    # Note: no disconnect yet — the handler stays parked in the read loop.
    ep = _endpoint(settings, registry)
    handle_task = asyncio.create_task(ep.handle(ws))

    try:
        # Wait for the initial registration, then for the in-place update.
        for _ in range(200):
            s = registry.get("alice")
            if s and s.client_id == "c-999":
                break
            await asyncio.sleep(0.01)
        session = registry.get("alice")
        assert session is not None
        assert session.client_id == "c-999"
        assert session.write_enabled is True
        assert session.allowed_dirs == ["/x"]
    finally:
        ws.feed_disconnect()
        await asyncio.wait_for(handle_task, timeout=2)


@pytest.mark.asyncio
async def test_idle_timeout_closes_session(settings, registry):
    settings.idle_timeout = 0.2
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed(REGISTER)
    # No further frames: the read loop must close with 4408 after the idle
    # timeout, then the handler returns.
    ep = _endpoint(settings, registry)
    await asyncio.wait_for(ep.handle(ws), timeout=3)
    assert ws.closed_code == 4408
    assert registry.get("alice") is None


@pytest.mark.asyncio
async def test_unsolicited_response_ignored(settings, registry):
    ws = FakeWebSocket(query={"token": "btok-alice"})
    ws.feed(REGISTER)
    ws.feed({"id": 12345, "result": {"bogus": True}})  # no pending call
    ws.feed_disconnect()
    ep = _endpoint(settings, registry)
    await asyncio.wait_for(ep.handle(ws), timeout=2)  # must not raise


@pytest.mark.asyncio
async def test_two_users_isolated(settings, registry):
    ws_a = FakeWebSocket(query={"token": "btok-alice"})
    ws_a.feed(REGISTER)
    ws_b = FakeWebSocket(query={"token": "btok-bob"})
    ws_b.feed({**REGISTER, "token": "btok-bob", "client_id": "b-1"})

    ep = _endpoint(settings, registry)
    ta = asyncio.create_task(ep.handle(ws_a))
    tb = asyncio.create_task(ep.handle(ws_b))

    for _ in range(100):
        if registry.get("alice") and registry.get("bob"):
            break
        await asyncio.sleep(0.01)
    assert registry.get("alice") is not None
    assert registry.get("bob") is not None
    assert registry.get("alice") is not registry.get("bob")

    # A call for bob must go out on bob's socket only.
    call = asyncio.create_task(registry.call("bob", "list_directory", {"path": "."}))
    for _ in range(100):
        if ws_b.outbox:
            break
        await asyncio.sleep(0.01)
    assert ws_a.outbox == []
    req = ws_b.outbox[0]
    ws_b.feed({"id": req["id"], "result": {"path": ".", "entries": []}})
    await asyncio.wait_for(call, timeout=2)

    ws_a.feed_disconnect()
    ws_b.feed_disconnect()
    await asyncio.gather(ta, tb)
