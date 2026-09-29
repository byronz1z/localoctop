"""Cross-stack integration test: the *real* Go bridge binary (built from
../localfsbridge) talks over a real WebSocket to the *real* Python adapter,
driven through httpx against the assembled FastAPI app.

This proves the interface contract in 任务书 §三 holds on both sides:
  * client dials ws://.../mcp-localfs/ws?token=...
  * register frame authenticates and binds the session
  * MCP initialize / tools/list / tools/call round-trip
  * traversal is denied end-to-end with code 4001

Skipped automatically when the Go toolchain or the bridge source tree is not
available (unit tests elsewhere still cover both sides).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from mcp_localfs.app import create_app
from mcp_localfs.config import Settings
from mcp_localfs.tokens import KIND_BRIDGE, KIND_MCP, TokenStore

REPO_ROOT = Path(__file__).resolve().parent.parent
# The bridge source lives at the octop-local-bridge repo root (adapter/ is a
# subdirectory of it); LOCALFSBRIDGE_DIR overrides for unusual layouts.
BRIDGE_DIR = Path(os.environ.get("LOCALFSBRIDGE_DIR", str(REPO_ROOT.parent)))


def _it_token_store() -> TokenStore:
    """The integration test runs one legacy-style deployment: the same
    secret seeded in both directions (the documented back-compat mode), so
    the Go bridge (-token it-token) and the MCP probe (Bearer it-token)
    both authenticate."""
    store = TokenStore()
    store.seed(KIND_MCP, "alice", "it-token")
    store.seed(KIND_BRIDGE, "alice", "it-token")
    return store


def _go_available() -> bool:
    return shutil.which("go") is not None


pytestmark = pytest.mark.skipif(
    not (_go_available() and BRIDGE_DIR.is_dir()),
    reason="go toolchain or ../localfsbridge source not available",
)


@pytest.fixture(scope="module")
def bridge_binary():
    """Build cmd/bridge once per module into a temp dir."""
    out_dir = tempfile.mkdtemp(prefix="lfsb-bin-")
    exe = os.path.join(out_dir, "bridge" + (".exe" if os.name == "nt" else ""))
    proc = subprocess.run(
        ["go", "build", "-o", exe, "./cmd/bridge"],
        cwd=str(BRIDGE_DIR),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        pytest.skip(f"go build failed: {proc.stderr}")
    yield exe
    shutil.rmtree(out_dir, ignore_errors=True)


async def _start_ws_server(app, host: str, port: int):
    """Run the FastAPI app on a real TCP port via uvicorn in this loop."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            ws_ping_interval=None, ws_ping_timeout=None)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # Wait for startup.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise RuntimeError("uvicorn did not start")
    return server, task


@pytest.mark.asyncio
async def test_full_stack_read_and_traversal_denial(bridge_binary, tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "hello.txt").write_text("full-stack-ok", encoding="utf-8")
    (workdir / "sub").mkdir()
    (workdir / "sub" / "note.md").write_text("# note", encoding="utf-8")
    audit_path = tmp_path / "audit.log"

    settings = Settings(
        token_store=_it_token_store(),
        allow_write=False,
        bridge_timeout=10.0,
        idle_timeout=60.0,
        audit_log_path=str(tmp_path / "server-audit.jsonl"),
        log_level="WARNING",
    )
    app = create_app(settings)

    # Find a free port.
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server, server_task = await _start_ws_server(app, "127.0.0.1", port)

    bridge_proc = await asyncio.create_subprocess_exec(
        bridge_binary,
        # The published cmd/bridge serves tool calls from flags only in
        # --headless mode (default mode is the desktop console+tray app).
        "-headless",
        "-server", f"ws://127.0.0.1:{port}/mcp-localfs/ws",
        "-token", "it-token",
        "-dir", str(workdir),
        "-audit", str(audit_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            auth = {"Authorization": "Bearer it-token",
                    "Accept": "application/json",
                    "Content-Type": "application/json"}

            # Wait for the bridge to register.
            registered = False
            for _ in range(100):
                r = await client.get("/healthz")
                if r.json().get("bridges_connected", 0) >= 1:
                    registered = True
                    break
                await asyncio.sleep(0.1)
            assert registered, "bridge did not register within 10s"

            # MCP handshake (SDK transport: initialize -> Mcp-Session-Id ->
            # notifications/initialized).
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "octop-it", "version": "1"}}})
            assert r.status_code == 200
            assert r.json()["result"]["serverInfo"]["name"] == "mcp-localfs"
            sid = r.headers.get("mcp-session-id")
            assert sid, "initialize must return Mcp-Session-Id"
            auth = {**auth, "Mcp-Session-Id": sid}
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "method": "notifications/initialized"})
            assert r.status_code == 202

            # tools/list -> exactly 4 read-only tools.
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = r.json()["result"]["tools"]
            assert sorted(t["name"] for t in tools) == [
                "get_file_info", "list_directory", "read_file", "search_files"]

            # read_file through the whole stack.
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "hello.txt"}}})
            body = r.json()
            assert "error" not in body, body
            text = body["result"]["content"][0]["text"]
            assert "full-stack-ok" in text
            assert body["result"]["isError"] is False

            # list_directory.
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "list_directory", "arguments": {"path": "."}}})
            listing = json.dumps(r.json()["result"]["content"][0]["text"])
            assert "hello.txt" in listing

            # search_files.
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "search_files",
                           "arguments": {"path": ".", "pattern": "*.md"}}})
            assert "note.md" in r.json()["result"]["content"][0]["text"]

            # Traversal denial must travel the full stack as an isError tool
            # result carrying the client's 4001 message.
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "read_file",
                           "arguments": {"path": "../../../etc/passwd"}}})
            body = r.json()
            assert "error" not in body
            assert body["result"]["isError"] is True
            assert "traversal" in body["result"]["content"][0]["text"].lower() \
                or "not allowed" in body["result"]["content"][0]["text"].lower()

            # Absolute path outside the whitelist.
            outside = str(Path(sys.executable))
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": outside}}})
            body = r.json()
            assert body["result"]["isError"] is True

            # write_file denied while the switch is off.
            r = await client.post("/mcp/localfs/", headers=auth, json={
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "write_file",
                           "arguments": {"path": "x.txt", "content": "no"}}})
            assert r.json()["error"]["code"] == -32601  # method not found (gated)

            # Client-side audit log recorded allow + deny decisions.
            for _ in range(50):
                if audit_path.exists():
                    content = audit_path.read_text(encoding="utf-8", errors="replace")
                    if '"decision":"deny"' in content and '"decision":"allow"' in content:
                        break
                await asyncio.sleep(0.1)
            content = audit_path.read_text(encoding="utf-8", errors="replace")
            assert '"decision":"allow"' in content
            assert '"decision":"deny"' in content

            # Server-side audit log too.
            srv_audit = Path(settings.audit_log_path)
            assert srv_audit.exists()
            assert '"decision": "allow"' in srv_audit.read_text(encoding="utf-8")
    finally:
        with contextlib.suppress(ProcessLookupError):
            bridge_proc.kill()
        await bridge_proc.wait()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5)
