"""Unit tests for the MCP streamable-HTTP endpoint (initialize / tools/list /
tools/call), auth, and per-user session isolation — exercised end-to-end
through httpx.ASGITransport against the real FastAPI app.

Since the endpoint is served by the official MCP SDK (StreamableHTTPSession
Manager), tests perform the spec handshake first: POST initialize -> capture
the Mcp-Session-Id response header -> notifications/initialized -> then
tools/list / tools/call with the session id attached.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from localoctop.app import create_app
from localoctop.config import Settings
from localoctop.errors import (
    CODE_METHOD_NOT_FOUND,
    CODE_NOT_ALLOWED,
    CODE_NO_BRIDGE,
)

from conftest import FakeWebSocket, bridge_pair_id, make_session, make_token_store

INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "octop", "version": "1"},
}

JSON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _settings(**over) -> Settings:
    base = dict(
        token_store=make_token_store(),
        allow_write=False,
        bridge_timeout=2.0,
        idle_timeout=30.0,
        audit_log_path="",
        log_level="WARNING",
    )
    base.update(over)
    return Settings(**base)


async def _mk(settings: Settings):
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


def _auth(token: str) -> dict:
    return {**JSON_HEADERS, "Authorization": f"Bearer {token}"}


def _pair(app, user: str) -> str:
    """The pair id of `user`'s tokens — what a real bridge WS registration
    records on its session (v0.6.1 pairing model)."""
    return bridge_pair_id(app.state.settings.token_store, user, f"btok-{user}")


async def _handshake(client: httpx.AsyncClient, token: str) -> dict:
    """Run the MCP handshake and return the headers (incl. Mcp-Session-Id)
    that subsequent requests must carry."""
    r = await client.post("/mcp/localoctop/", headers=_auth(token), json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
    })
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid, "initialize must return the Mcp-Session-Id header"
    headers = {**_auth(token), "Mcp-Session-Id": sid}
    r = await client.post("/mcp/localoctop/", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert r.status_code == 202
    return headers


@pytest.mark.asyncio
async def test_healthz():
    client, _ = await _mk(_settings())
    async with client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["bridges_connected"] == 0


@pytest.mark.asyncio
async def test_initialize_handshake():
    client, _ = await _mk(_settings())
    async with client:
        r = await client.post("/mcp/localoctop/", headers=_auth("tok-alice"), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 1
        assert body["result"]["protocolVersion"] == "2024-11-05"
        assert body["result"]["serverInfo"]["name"] == "localoctop"
        assert "tools" in body["result"]["capabilities"]
        assert r.headers.get("mcp-session-id"), "Mcp-Session-Id header required"


@pytest.mark.asyncio
async def test_initialize_without_trailing_slash():
    """Connector probes may omit the trailing slash; both must work."""
    client, _ = await _mk(_settings())
    async with client:
        r = await client.post("/mcp/localoctop", headers=_auth("tok-alice"), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 200
        assert "result" in r.json()


@pytest.mark.asyncio
async def test_tools_list_returns_readonly_tools():
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        assert names == sorted([
            "get_file_info", "list_allowed_directories", "list_directory",
            "list_directory_with_sizes", "directory_tree", "read_file",
            "read_media_file", "read_multiple_files", "search_files", "zip_files",
        ])
        for t in tools:
            assert "inputSchema" in t and t["inputSchema"]["type"] == "object"
            # v0.5.0: annotations ride to the model per the MCP spec.
            assert t.get("annotations", {}).get("openWorldHint") is False


@pytest.mark.asyncio
async def test_tools_list_includes_write_when_enabled():
    client, _ = await _mk(_settings(allow_write=True))
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {"write_file", "create_directory", "edit_file", "move_file",
                "delete_file", "remove_directory", "unzip_file"} <= names
        assert len(names) == 17


@pytest.mark.asyncio
async def test_auth_required():
    client, _ = await _mk(_settings())
    async with client:
        r = await client.post("/mcp/localoctop/", headers=JSON_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 401

        r = await client.post("/mcp/localoctop/", headers=_auth("wrong-token"), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 401
        assert r.json()["error"]["code"] == 4102


@pytest.mark.asyncio
async def test_bridge_token_rejected_on_mcp_bearer():
    """任务书 v1.1 §七 分向拒绝 1/2：a valid *bridge* token (client WSS)
    must not pass the MCP connector Bearer check — the two token kinds are
    separate namespaces."""
    client, _ = await _mk(_settings())
    async with client:
        # btok-alice is alice's valid bridge token; as an MCP Bearer it is
        # just an unknown token.
        r = await client.post("/mcp/localoctop/", headers=_auth("btok-alice"), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS,
        })
        assert r.status_code == 401
        assert r.json()["error"]["code"] == 4102


@pytest.mark.asyncio
async def test_session_id_required_after_handshake():
    """The SDK enforces the Mcp-Session-Id header on every request after
    initialize; a missing or unknown id must be rejected."""
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        # Missing session id -> rejected.
        r = await client.post("/mcp/localoctop/", headers=_auth("tok-alice"), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert r.status_code in (400, 404)
        # Unknown session id -> 404.
        r = await client.post("/mcp/localoctop/",
                              headers={**_auth("tok-alice"), "Mcp-Session-Id": "deadbeef"},
                              json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        assert r.status_code == 404
        # Correct session id -> served.
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
        })
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_session_bound_to_credential():
    """A session created with alice's token must not be usable with bob's
    token (SDK session ownership), even though bob is a valid user."""
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        sid = headers["Mcp-Session-Id"]
        r = await client.post("/mcp/localoctop/",
                              headers={**_auth("tok-bob"), "Mcp-Session-Id": sid},
                              json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_tools_call_no_bridge_connected():
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "a.txt"}},
        })
        assert r.status_code == 200
        err = r.json()["error"]
        assert err["code"] == CODE_NO_BRIDGE


@pytest.mark.asyncio
async def test_tools_call_routes_to_bridge_and_renders_text():
    """A full tools/call: MCP request -> bridge frame -> reply -> MCP result."""
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    session = make_session("alice", ws, pair_id=_pair(app, "alice"))
    await registry.register(session)

    async def bridge_responder():
        # Wait for the forwarded request, then answer it.
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        assert req["method"] == "read_file"
        assert req["params"] == {"path": "hello.txt"}
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "path": "hello.txt", "text": "hi there\n", "encoding": "utf-8",
            "size": 9, "truncated": False,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(bridge_responder())
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "hello.txt"}},
        })
        await task
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body
        content = body["result"]["content"]
        assert content[0]["type"] == "text"
        assert "hi there" in content[0]["text"]
        assert body["result"]["isError"] is False


@pytest.mark.asyncio
async def test_tools_call_client_denial_becomes_isError():
    """A bridge-side denial (e.g. traversal) is a tool result, not a protocol
    error, per MCP: isError=true with the message."""
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        registry.deliver("alice", req["id"], {"id": req["id"], "error": {
            "code": CODE_NOT_ALLOWED,
            "message": 'path traversal ("..") is not allowed'}})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "../../etc/passwd"}},
        })
        await task
        body = r.json()
        assert "error" not in body
        assert body["result"]["isError"] is True
        assert "traversal" in body["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_user_isolation_bob_cannot_reach_alice_bridge():
    """The core server-side security invariant: a call authenticated as bob
    must never be served by alice's session."""
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    # alice's session is registered with HER pair id — the strongest form of
    # the invariant: even a paired, online bridge of alice must not serve
    # bob, because bob's mcp token routes to bob's pair only.
    await registry.register(make_session("alice", ws, client_id="alice-c1",
                                         pair_id=_pair(app, "alice")))

    async with client:
        headers = await _handshake(client, "tok-bob")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "secret.txt"}},
        })
        body = r.json()
        # bob has no bridge -> CODE_NO_BRIDGE, and nothing was sent to alice.
        assert body["error"]["code"] == CODE_NO_BRIDGE
        assert ws.outbox == []


@pytest.mark.asyncio
async def test_write_tool_denied_while_disabled():
    client, app = await _mk(_settings(allow_write=False))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}},
        })
        body = r.json()
        assert body["error"]["code"] == CODE_METHOD_NOT_FOUND
        assert ws.outbox == []


@pytest.mark.asyncio
async def test_write_tool_gated_by_client_session_flag():
    """Server writes on, but the client session registered read-only: the
    call must be refused as a tool error before hitting the wire."""
    client, app = await _mk(_settings(allow_write=True))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, write_enabled=False, pair_id=_pair(app, "alice")))
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}},
        })
        body = r.json()
        assert body["result"]["isError"] is True
        assert "write switch off" in body["result"]["content"][0]["text"]
        assert ws.outbox == []


@pytest.mark.asyncio
async def test_admin_killswitch_disables_everything():
    client, app = await _mk(_settings(disabled=True))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "a.txt"}},
        })
        assert r.json()["error"]["code"] == CODE_NOT_ALLOWED
        assert ws.outbox == []


@pytest.mark.asyncio
async def test_unknown_method_and_tool():
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 10, "method": "resources/list",
        })
        assert r.json()["error"]["code"] == CODE_METHOD_NOT_FOUND

        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "rm_rf", "arguments": {}},
        })
        assert r.json()["error"]["code"] == CODE_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_notification_gets_202():
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/roots/list_changed",
        })
        assert r.status_code == 202


@pytest.mark.asyncio
async def test_invalid_json_body():
    client, _ = await _mk(_settings())
    async with client:
        r = await client.post(
            "/mcp/localoctop/",
            headers={**_auth("tok-alice"), "Content-Type": "application/json"},
            content=b"{not json",
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_delete_terminates_session():
    """DELETE ends the MCP session per the SDK; the session id then 404s."""
    client, _ = await _mk(_settings())
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.delete("/mcp/localoctop/", headers=headers)
        assert r.status_code == 200
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_sse_stream_requires_session():
    """GET opens the server->client SSE stream per the SDK; without a valid
    session id it must be rejected (400/404), not silently served."""
    client, _ = await _mk(_settings())
    async with client:
        r = await client.get("/mcp/localoctop/",
                             headers={**_auth("tok-alice"), "Accept": "text/event-stream"})
        assert r.status_code in (400, 404)


@pytest.mark.asyncio
async def test_binary_read_rendered_as_resource_blob():
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "path": "img.png", "base64": "aGk=", "encoding": "base64",
            "size": 2, "truncated": False,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "img.png"}},
        })
        await task
        content = r.json()["result"]["content"][0]
        assert content["type"] == "resource"
        assert content["resource"]["blob"] == "aGk="
        assert content["resource"]["mimeType"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_bridge_timeout_surfaces_as_error_result():
    client, app = await _mk(_settings(bridge_timeout=0.2))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))
    async with client:
        headers = await _handshake(client, "tok-alice")
        r = await client.post("/mcp/localoctop/", headers=headers, json={
            "jsonrpc": "2.0", "id": 13, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "a.txt"}},
        })
        body = r.json()
        # Timeout is a tool-level failure: isError result with code 4005 detail.
        assert body["result"]["isError"] is True
        assert "within" in body["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# v0.5.0 render + gating tests
# ---------------------------------------------------------------------------

async def _call_tool(client, headers, req_id, name, arguments):
    r = await client.post("/mcp/localoctop/", headers=headers, json={
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return r.json()


@pytest.mark.asyncio
async def test_read_media_file_rendered_as_typed_resource():
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        assert req["method"] == "read_media_file"
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "path": "photo.jpg", "base64": "aGk=", "mime": "image/jpeg", "size": 2,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        body = await _call_tool(client, headers, 20, "read_media_file", {"path": "photo.jpg"})
        await task
        blocks = body["result"]["content"]
        res = next(b for b in blocks if b["type"] == "resource")
        assert res["resource"]["mimeType"] == "image/jpeg"
        assert res["resource"]["blob"] == "aGk="


@pytest.mark.asyncio
async def test_zip_files_rendered_as_zip_resource():
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "archive": "docs", "base64": "UEsDBA==", "size": 123, "entries": 2, "skipped": 1,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        body = await _call_tool(client, headers, 21, "zip_files", {"paths": ["docs"]})
        await task
        blocks = body["result"]["content"]
        res = next(b for b in blocks if b["type"] == "resource")
        assert res["resource"]["mimeType"] == "application/zip"
        note = next(b for b in blocks if b["type"] == "text")
        assert "2 entries" in note["text"] and "1 skipped" in note["text"]


@pytest.mark.asyncio
async def test_read_multiple_files_mixed_success_and_failure():
    client, app = await _mk(_settings())
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "files": [
                {"path": "ok.txt", "text": "contents here", "encoding": "utf-8"},
                {"path": "missing.txt", "error": "not found"},
            ],
            "failed": 1, "truncated": False,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        body = await _call_tool(client, headers, 22, "read_multiple_files", {"paths": ["ok.txt", "missing.txt"]})
        await task
        texts = [b["text"] for b in body["result"]["content"] if b["type"] == "text"]
        assert any("contents here" in t for t in texts)
        assert any("ERROR" in t for t in texts)
        assert any("1 file(s) failed" in t for t in texts)


@pytest.mark.asyncio
async def test_edit_file_rendered_with_diff():
    client, app = await _mk(_settings(allow_write=True))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, write_enabled=True, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        assert req["method"] == "edit_file"
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "path": "a.txt", "applied": True, "dry_run": False,
            "edits_applied": 1, "matches": 1, "diff": "-old\n+new\n",
            "size_before": 3, "size_after": 3,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        body = await _call_tool(client, headers, 23, "edit_file", {
            "path": "a.txt", "edits": [{"oldText": "old", "newText": "new"}],
        })
        await task
        text = body["result"]["content"][0]["text"]
        assert "-old" in text and "+new" in text and "edit_file a.txt" in text


@pytest.mark.asyncio
async def test_delete_file_refused_when_client_write_off():
    # Server allows writes, but the employee's client registered with the
    # write toggle OFF -> write-class tools must be refused before fan-out.
    client, app = await _mk(_settings(allow_write=True))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, write_enabled=False, pair_id=_pair(app, "alice")))
    async with client:
        headers = await _handshake(client, "tok-alice")
        body = await _call_tool(client, headers, 24, "delete_file", {"path": "x.txt"})
        assert body["result"]["isError"] is True
        assert "write switch off" in body["result"]["content"][0]["text"]
        assert ws.outbox == []  # never forwarded to the machine


@pytest.mark.asyncio
async def test_unzip_file_routes_when_writes_on():
    client, app = await _mk(_settings(allow_write=True))
    registry = app.state.registry
    ws = FakeWebSocket()
    await registry.register(make_session("alice", ws, write_enabled=True, pair_id=_pair(app, "alice")))

    async def responder():
        for _ in range(50):
            if ws.outbox:
                break
            await asyncio.sleep(0.02)
        req = ws.outbox[0]
        registry.deliver("alice", req["id"], {"id": req["id"], "result": {
            "archive": "b.zip", "dest": "out", "extracted": 3, "total_bytes": 900,
        }})

    async with client:
        headers = await _handshake(client, "tok-alice")
        task = asyncio.create_task(responder())
        body = await _call_tool(client, headers, 25, "unzip_file", {"archive": "b.zip", "dest": "out"})
        await task
        assert body["result"]["isError"] is False
        assert "extracted" in body["result"]["content"][0]["text"]
