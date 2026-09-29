#!/usr/bin/env python3
"""Minimal self-verification for mcp-localfs — runs without pytest.

Covers acceptance criteria #1 and #3 at smoke level:
  * app assembles and /healthz answers
  * MCP initialize handshake works with Bearer auth
  * tools/list returns exactly the 4 read-only tools
  * auth is enforced (missing/bad token -> 401)
  * a tools/call with no bridge connected returns the 4101 error
  * a tools/call round-trips through a scripted fake bridge session
  * path-traversal denial from the client renders as an isError result
  * audit log records allow + deny decisions

Usage:
    python scripts/verify.py

Exits 0 on success, 1 on any failure (prints a PASS/FAIL summary).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Make the package importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def main() -> int:
    import httpx

    from mcp_localfs.app import create_app
    from mcp_localfs.audit import AuditLog
    from mcp_localfs.config import Settings
    from mcp_localfs.sessions import BridgeSession, SessionRegistry
    from mcp_localfs.tokens import KIND_BRIDGE, KIND_MCP, TokenStore
    from mcp_localfs.tools import ToolService

    tmp = Path(tempfile.mkdtemp(prefix="lfsb-verify-"))
    audit_path = tmp / "calls.jsonl"

    store = TokenStore()
    store.seed(KIND_MCP, "alice", "tok-alice")
    store.seed(KIND_MCP, "bob", "tok-bob")
    store.seed(KIND_BRIDGE, "alice", "btok-alice")
    store.seed(KIND_BRIDGE, "bob", "btok-bob")
    settings = Settings(
        token_store=store,
        allow_write=False,
        bridge_timeout=2.0,
        idle_timeout=30.0,
        audit_log_path=str(audit_path),
        log_level="WARNING",
    )
    app = create_app(settings)
    registry: SessionRegistry = app.state.registry
    audit: AuditLog = app.state.audit
    auth = {"Authorization": "Bearer tok-alice", "Accept": "application/json"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
        # 1) healthz
        r = await client.get("/healthz")
        check("healthz 200", r.status_code == 200 and r.json()["status"] == "ok", str(r.status_code))

        # 2) initialize handshake (SDK transport mints the Mcp-Session-Id)
        r = await client.post("/mcp/localfs/", headers=auth, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {}, "clientInfo": {"name": "verify", "version": "1"}}})
        body = r.json() if r.status_code == 200 else {}
        sid = r.headers.get("mcp-session-id", "")
        check("initialize handshake",
              r.status_code == 200 and body.get("result", {}).get("serverInfo", {}).get("name") == "mcp-localfs" and sid,
              json.dumps(body)[:200])
        session_headers = {**auth, "Mcp-Session-Id": sid}

        # 3) tools/list -> exactly 4 read-only tools
        r = await client.post("/mcp/localfs/", headers=session_headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = sorted(t["name"] for t in r.json().get("result", {}).get("tools", []))
        check("tools/list = 4 read-only tools",
              names == ["get_file_info", "list_directory", "read_file", "search_files"],
              str(names))

        # 4) auth enforced
        r = await client.post("/mcp/localfs/", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        check("auth required (401)", r.status_code == 401, str(r.status_code))
        r = await client.post("/mcp/localfs/", headers={"Authorization": "Bearer nope"},
                              json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        check("bad token rejected (401)", r.status_code == 401, str(r.status_code))

        # 5) no bridge connected -> 4101
        r = await client.post("/mcp/localfs/", headers=session_headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "a.txt"}}})
        check("no-bridge error 4101", r.json().get("error", {}).get("code") == 4101, json.dumps(r.json())[:200])

    # 6) full tools/call round-trip via a scripted fake bridge session
    class FakeWS:
        def __init__(self):
            self.sent: list[dict] = []
            self.reply: dict | None = None

        async def send_json(self, data):
            self.sent.append(data)

        async def close(self, code=1000, reason=None):
            pass

    fake = FakeWS()
    session = BridgeSession(user_id="alice", client_id="verify-1", websocket=fake,
                            hostname="verify", allowed_dirs=["/tmp/zbs"], write_enabled=False)
    await registry.register(session)

    async def answer(delay_result: dict):
        # Wait for the request to be written, then resolve it via the registry.
        for _ in range(200):
            if fake.sent:
                break
            await asyncio.sleep(0.01)
        req = fake.sent[-1]
        registry.deliver("alice", req["id"], {"id": req["id"], "result": delay_result})

    svc = ToolService(settings, registry, audit)
    task = asyncio.create_task(answer({"path": "hello.txt", "text": "verify-ok\n",
                                       "encoding": "utf-8", "size": 10, "truncated": False}))
    out = await svc.call_tool("alice", "read_file", {"path": "hello.txt"})
    await task
    text = out["content"][0]["text"]
    check("tools/call round-trip", out.get("isError") is False and "verify-ok" in text, text[:200])

    # 7) traversal denial from client -> isError result
    async def answer_error(code: int, message: str):
        for _ in range(200):
            if len(fake.sent) > 1:
                break
            await asyncio.sleep(0.01)
        req = fake.sent[-1]
        registry.deliver("alice", req["id"], {"id": req["id"], "error": {"code": code, "message": message}})

    task = asyncio.create_task(answer_error(4001, 'path traversal ("..") is not allowed'))
    out = await svc.call_tool("alice", "read_file", {"path": "../../etc/passwd"})
    await task
    check("traversal denial -> isError 4001",
          out.get("isError") is True and "traversal" in out["content"][0]["text"],
          json.dumps(out)[:200])

    # 8) user isolation: bob's call must not touch alice's socket
    try:
        await svc.call_tool("bob", "read_file", {"path": "x"})
        isolated = False
    except Exception as exc:  # BridgeProtocolError 4101 expected
        isolated = getattr(exc, "code", None) == 4101
    check("per-user isolation (bob != alice)", isolated and len(fake.sent) == 2,
          f"sent={len(fake.sent)}")

    # 9) audit log has allow + deny records
    await asyncio.sleep(0.05)
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines() if audit_path.exists() else []
    decisions = [json.loads(x).get("decision") for x in lines]
    check("audit log allow+deny", "allow" in decisions and "deny" in decisions, str(decisions))

    print()
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
