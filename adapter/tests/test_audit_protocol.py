"""Tests for the server-side audit log and the wire protocol helpers."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from mcp_localfs.audit import AuditLog
from mcp_localfs.errors import BridgeProtocolError, CODE_INVALID_PARAMS, CODE_METHOD_NOT_FOUND, CODE_PARSE_ERROR
from mcp_localfs.protocol import (
    READ_ONLY_TOOLS,
    parse_request,
    tools_list,
    validate_tool_name,
)


# ---------------------------------------------------------------- audit log

@pytest.mark.asyncio
async def test_audit_writes_jsonl(tmp_path):
    path = str(tmp_path / "logs" / "calls.jsonl")
    log = AuditLog(path)
    await log.record(user_id="alice", method="read_file", path="a.txt",
                     decision="allow", code=0, duration_ms=1.2, client_id="c1")
    await log.record(user_id="alice", method="read_file", path="../evil",
                     decision="deny", code=4001, detail="traversal")
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[1])
    assert rec["user"] == "alice"
    assert rec["decision"] == "deny"
    assert rec["code"] == 4001
    assert rec["detail"] == "traversal"
    assert rec["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_audit_rotation(tmp_path):
    path = str(tmp_path / "calls.jsonl")
    log = AuditLog(path, max_bytes=1)  # rotate on every append
    for i in range(4):
        await log.record(user_id="u", method="m", decision="allow", code=0,
                         detail=f"line-{i}")
    assert os.path.exists(path + ".1")
    # Active file holds only the most recent record.
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_audit_empty_path_is_noop():
    log = AuditLog("")
    await log.record(user_id="u", method="m", decision="allow", code=0)
    # Nothing to assert beyond "did not raise".


@pytest.mark.asyncio
async def test_audit_concurrent_writes_are_line_atomic(tmp_path):
    path = str(tmp_path / "calls.jsonl")
    log = AuditLog(path)

    async def writer(i: int):
        for j in range(10):
            await log.record(user_id=f"u{i}", method="m", decision="allow",
                             code=0, detail="x" * 50)

    await asyncio.gather(*(writer(i) for i in range(5)))
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 50
    for line in lines:
        json.loads(line)  # every line must be intact JSON


# ---------------------------------------------------------------- protocol

def test_parse_request_valid():
    req = parse_request(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}')
    assert req["method"] == "tools/call"


def test_parse_request_invalid():
    with pytest.raises(BridgeProtocolError) as ei:
        parse_request(b"{not json")
    assert ei.value.code == CODE_PARSE_ERROR
    with pytest.raises(BridgeProtocolError) as ei:
        parse_request(b"[1,2,3]")
    assert ei.value.code == CODE_PARSE_ERROR
    with pytest.raises(BridgeProtocolError) as ei:
        parse_request(b'{"id":1}')
    assert ei.value.code == CODE_INVALID_PARAMS


def test_tools_list_readonly_contract():
    tools = tools_list(write_enabled=False)
    names = [t["name"] for t in tools]
    assert names == list(READ_ONLY_TOOLS)
    assert len(tools) == 4
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert "properties" in t["inputSchema"]
        assert t["description"]


def test_tools_list_write_enabled():
    names = {t["name"] for t in tools_list(write_enabled=True)}
    assert names == set(READ_ONLY_TOOLS) | {"write_file", "create_directory"}


def test_validate_tool_name():
    validate_tool_name("read_file", write_enabled=False)
    with pytest.raises(BridgeProtocolError) as ei:
        validate_tool_name("write_file", write_enabled=False)
    assert ei.value.code == CODE_METHOD_NOT_FOUND
    validate_tool_name("write_file", write_enabled=True)
    with pytest.raises(BridgeProtocolError) as ei:
        validate_tool_name("format_disk", write_enabled=True)
    assert ei.value.code == CODE_METHOD_NOT_FOUND
