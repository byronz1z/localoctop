#!/usr/bin/env python3
"""MCP client probe — stage 3 of the end-to-end check (scripts/verify_e2e.ps1).

Speaks raw streamable-HTTP JSON-RPC against /mcp/localfs/ exactly the way
Octop's custom-MCP connector does (see Octop src/octop/infra/connectors/
probe.py: initialize -> notifications/initialized -> tools/list -> tools/call,
Bearer auth, Accept: application/json, text/event-stream).

Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:18080")
    ap.add_argument("--token", required=True, help="mcp_token (Octop connector Bearer)")
    ap.add_argument("--expect-file", required=True, help="file seeded in the whitelist dir")
    ap.add_argument("--expect-content", default="e2e-ok", help="substring expected in read_file")
    args = ap.parse_args()

    url = args.base_url.rstrip("/") + "/mcp/localfs/"
    base_headers = {
        "Authorization": f"Bearer {args.token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30) as client:
        # 0) auth is enforced: no token -> 401
        r = client.post(url, headers={"Accept": "application/json",
                                      "Content-Type": "application/json"},
                        json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "e2e-probe", "version": "1"}}})
        check("auth required (401 without token)", r.status_code == 401, str(r.status_code))

        # 1) initialize handshake
        r = client.post(url, headers=base_headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "e2e-probe", "version": "1"}}})
        body = r.json() if r.status_code == 200 else {}
        sid = r.headers.get("mcp-session-id", "")
        check("initialize -> 200 + serverInfo + Mcp-Session-Id",
              r.status_code == 200
              and body.get("result", {}).get("serverInfo", {}).get("name") == "mcp-localfs"
              and bool(sid),
              f"status={r.status_code} body={json.dumps(body)[:160]}")
        headers = {**base_headers, "Mcp-Session-Id": sid}

        # 2) initialized notification
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        check("notifications/initialized -> 202", r.status_code == 202, str(r.status_code))

        # 3) tools/list -> exactly the 4 read-only tools (write switch off)
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = r.json().get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        check("tools/list = 4 read-only tools",
              names == ["get_file_info", "list_directory", "read_file", "search_files"],
              str(names))

        # 4) tools/call list_directory -> real directory entries
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_directory", "arguments": {"path": "."}}})
        body = r.json()
        result = body.get("result", {})
        text = ""
        if isinstance(result.get("content"), list) and result["content"]:
            text = str(result["content"][0].get("text", ""))
        check("tools/call list_directory returns seeded entry",
              "error" not in body and result.get("isError") is False
              and args.expect_file in text,
              json.dumps(body)[:200])

        # 5) tools/call read_file -> real file content
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": args.expect_file}}})
        body = r.json()
        result = body.get("result", {})
        text = ""
        if isinstance(result.get("content"), list) and result["content"]:
            text = str(result["content"][0].get("text", ""))
        check("tools/call read_file returns real content",
              "error" not in body and result.get("isError") is False
              and args.expect_content in text,
              json.dumps(body)[:200])

        # 6) traversal denial travels the full stack as isError
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "../../../boot.ini"}}})
        body = r.json()
        result = body.get("result", {})
        text = ""
        if isinstance(result.get("content"), list) and result["content"]:
            text = str(result["content"][0].get("text", ""))
        check("path traversal denied end-to-end (isError)",
              "error" not in body and result.get("isError") is True
              and ("traversal" in text.lower() or "not allowed" in text.lower()),
              json.dumps(body)[:200])

        # 7) write tool gated while the switch is off -> -32601
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "write_file", "arguments": {"path": "x.txt", "content": "no"}}})
        body = r.json()
        check("write_file gated (-32601) while writes off",
              body.get("error", {}).get("code") == -32601, json.dumps(body)[:200])

    print()
    print(f"PROBE RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
