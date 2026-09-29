"""Shared pytest fixtures and an in-process fake WebSocket for adapter tests.

The fake WebSocket implements the small slice of Starlette's WebSocket API
the adapter uses (send_json, receive_json, close, query_params, headers), so
we can unit-test the bridge endpoint and session routing without real sockets.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from localoctop.audit import AuditLog
from localoctop.config import Settings
from localoctop.sessions import SessionRegistry
from localoctop.tokens import KIND_BRIDGE, KIND_MCP, TokenStore


def make_token_store() -> TokenStore:
    """Two users, each with an mcp_token and a bridge_token. The two kinds
    are distinct secrets, so cross-direction misuse is testable."""
    store = TokenStore()
    store.seed(KIND_MCP, "alice", "tok-alice")
    store.seed(KIND_MCP, "bob", "tok-bob")
    store.seed(KIND_BRIDGE, "alice", "btok-alice")
    store.seed(KIND_BRIDGE, "bob", "btok-bob")
    return store


class FakeWebSocket:
    """A scriptable stand-in for starlette.WebSocket.

    `inbox` holds frames the server will read (receive_json pops from the
    front). `outbox` collects everything the server sends. When inbox is
    empty, receive_json blocks on an asyncio.Event so the read loop parks
    instead of spinning — tests push frames via `feed()`.
    """

    def __init__(self, inbox: list[Any] | None = None, headers: dict | None = None,
                 query: dict | None = None, client_host: str = "203.0.113.9"):
        self.inbox: asyncio.Queue = asyncio.Queue()
        for item in inbox or []:
            self.inbox.put_nowait(item)
        self.outbox: list[Any] = []
        self.headers = headers or {}
        self.query_params = query or {}
        self.closed_code: int | None = None
        self.client = _Client(client_host)
        self._wake = asyncio.Event()

    def feed(self, frame: Any) -> None:
        self.inbox.put_nowait(frame)

    def feed_disconnect(self, code: int = 1000) -> None:
        self.inbox.put_nowait(_Disconnect(code))

    async def accept(self) -> None:
        pass

    async def send_json(self, data: Any) -> None:
        self.outbox.append(data)

    async def receive_json(self) -> Any:
        item = await self.inbox.get()
        if isinstance(item, _Disconnect):
            raise WebSocketDisconnect(code=item.code)
        return item

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        # Record only the first close, like a real socket: the handler's
        # finally-block close(1000) must not mask an earlier policy close.
        if self.closed_code is None:
            self.closed_code = code


@dataclass
class _Client:
    host: str
    port: int = 12345


@dataclass
class _Disconnect:
    code: int = 1000


@pytest.fixture
def settings() -> Settings:
    """Split-token settings with two users, writes globally off.

    MCP tokens (Octop Bearer) and bridge tokens (client WSS) are distinct
    secrets per user, mirroring 任务书 v1.1 §七.
    """
    return Settings(
        token_store=make_token_store(),
        static_tokens={"alice": "tok-alice", "bob": "tok-bob"},
        allow_write=False,
        bridge_timeout=2.0,
        idle_timeout=5.0,
        audit_log_path="",
        log_level="WARNING",
    )


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry(request_timeout=2.0)


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog(path="")


@pytest.fixture
def collecting_audit() -> tuple[AuditLog, list[dict]]:
    """An AuditLog that captures records in a list for assertions."""
    records: list[dict] = []

    class _Capture(AuditLog):
        async def record(self, **kwargs: Any) -> None:  # type: ignore[override]
            records.append(kwargs)

    return _Capture(path=""), records


def make_session(user_id: str, ws: FakeWebSocket, client_id: str = "c1",
                 write_enabled: bool = False) -> Any:
    from localoctop.sessions import BridgeSession

    return BridgeSession(
        user_id=user_id,
        client_id=client_id,
        websocket=ws,
        hostname="test-host",
        allowed_dirs=["/home/test/Documents/Work"],
        write_enabled=write_enabled,
        version="1.0.0",
    )
