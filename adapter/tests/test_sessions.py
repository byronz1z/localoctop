"""Tests for the session registry: per-user routing, reconnect replacement,
timeout, and pending-call failure semantics.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_localfs.errors import BridgeProtocolError, CODE_NO_BRIDGE, CODE_TIMEOUT
from mcp_localfs.sessions import BridgeSession, SessionRegistry

from conftest import FakeWebSocket, make_session


@pytest.mark.asyncio
async def test_register_and_call_roundtrip():
    reg = SessionRegistry(request_timeout=2.0)
    ws = FakeWebSocket()
    await reg.register(make_session("alice", ws))

    async def responder():
        for _ in range(100):
            if ws.outbox:
                break
            await asyncio.sleep(0.01)
        req = ws.outbox[-1]
        assert req["method"] == "list_directory"
        # No read loop is running in this test, so resolve the pending future
        # the same way BridgeWSEndpoint would on receiving the frame.
        reg.deliver("alice", req["id"], {"id": req["id"], "result": {"path": ".", "entries": []}})

    task = asyncio.create_task(responder())
    result = await reg.call("alice", "list_directory", {"path": "."})
    await task
    assert result == {"path": ".", "entries": []}


@pytest.mark.asyncio
async def test_call_without_session_raises_no_bridge():
    reg = SessionRegistry()
    with pytest.raises(BridgeProtocolError) as ei:
        await reg.call("ghost", "read_file", {"path": "a"})
    assert ei.value.code == CODE_NO_BRIDGE


@pytest.mark.asyncio
async def test_call_timeout():
    reg = SessionRegistry(request_timeout=0.15)
    ws = FakeWebSocket()
    await reg.register(make_session("alice", ws))
    started = asyncio.get_event_loop().time()
    with pytest.raises(BridgeProtocolError) as ei:
        await reg.call("alice", "read_file", {"path": "a"})
    elapsed = asyncio.get_event_loop().time() - started
    assert ei.value.code == CODE_TIMEOUT
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_reconnect_replaces_session_and_fails_pending():
    reg = SessionRegistry(request_timeout=5.0)
    ws1 = FakeWebSocket()
    s1 = make_session("alice", ws1, client_id="old")
    await reg.register(s1)

    # Start a call that will never be answered by ws1.
    call_task = asyncio.create_task(reg.call("alice", "read_file", {"path": "a"}))
    await asyncio.sleep(0.05)
    assert len(ws1.outbox) == 1

    # Reconnect: a new socket registers for the same user.
    ws2 = FakeWebSocket()
    s2 = make_session("alice", ws2, client_id="new")
    await reg.register(s2)

    with pytest.raises(BridgeProtocolError) as ei:
        await call_task
    assert ei.value.code == CODE_NO_BRIDGE
    assert reg.get("alice") is s2
    assert ws1.closed_code == 4000  # stale socket was closed


@pytest.mark.asyncio
async def test_unregister_only_removes_current_session():
    reg = SessionRegistry()
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    s1 = make_session("alice", ws1, client_id="old")
    s2 = make_session("alice", ws2, client_id="new")
    await reg.register(s1)
    await reg.register(s2)
    # Late disconnect from the replaced socket must not drop the new one.
    await reg.unregister("alice", s1)
    assert reg.get("alice") is s2
    await reg.unregister("alice", s2)
    assert reg.get("alice") is None


@pytest.mark.asyncio
async def test_error_response_propagates_code_and_message():
    reg = SessionRegistry(request_timeout=2.0)
    ws = FakeWebSocket()
    await reg.register(make_session("alice", ws))

    async def responder():
        for _ in range(100):
            if ws.outbox:
                break
            await asyncio.sleep(0.01)
        req = ws.outbox[-1]
        reg.deliver("alice", req["id"], {"id": req["id"], "error": {"code": 4001, "message": "path not allowed"}})

    task = asyncio.create_task(responder())
    with pytest.raises(BridgeProtocolError) as ei:
        await reg.call("alice", "read_file", {"path": "../evil"})
    await task
    assert ei.value.code == 4001
    assert ei.value.message == "path not allowed"


@pytest.mark.asyncio
async def test_unmatched_response_ignored():
    reg = SessionRegistry()
    ws = FakeWebSocket()
    await reg.register(make_session("alice", ws))
    assert reg.deliver("alice", 999, {"id": 999, "result": {}}) is False
    assert reg.deliver("alice", "not-an-int", {"id": "x"}) is False
    assert reg.deliver("nobody", 1, {"id": 1}) is False


@pytest.mark.asyncio
async def test_ids_increase_monotonically_per_session():
    s = BridgeSession(user_id="u", client_id="c", websocket=FakeWebSocket())
    ids = [s.next_id() for _ in range(5)]
    assert ids == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_snapshot_shape():
    reg = SessionRegistry()
    ws = FakeWebSocket()
    await reg.register(make_session("alice", ws))
    snap = reg.snapshot()
    assert len(snap) == 1
    assert snap[0]["user_id"] == "alice"
    assert snap[0]["client_id"] == "c1"
    assert "websocket" not in snap[0]  # never leak the socket object
