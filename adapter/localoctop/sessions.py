"""Bridge session registry: maps user_id -> live WSS session.

The Go client dials in (outbound from the employee machine, NAT-friendly);
this registry is what lets the MCP endpoint route a tools/call to the right
employee machine. Invariant enforced here: **a user can only ever reach their
own registered session** — sessions are keyed strictly by authenticated
user_id, never by anything the caller supplies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import BridgeProtocolError, CODE_NO_BRIDGE, CODE_TIMEOUT

logger = logging.getLogger("localoctop.sessions")


@dataclass
class BridgeSession:
    """One connected employee bridge."""

    user_id: str
    client_id: str
    websocket: Any  # starlette WebSocket; typed loosely so tests can fake it
    hostname: str = ""
    allowed_dirs: list[str] = field(default_factory=list)
    write_enabled: bool = False
    version: str = ""
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    # Pending request futures, keyed by the integer id we sent on the wire.
    _pending: dict[int, asyncio.Future] = field(default_factory=dict, repr=False)
    _id_counter: int = field(default=0, repr=False)

    def touch(self) -> None:
        self.last_seen = time.time()

    def next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def fail_pending(self, exc: BaseException) -> None:
        """Fail every in-flight request future (used at teardown/replacement)."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def info(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "client_id": self.client_id,
            "hostname": self.hostname,
            "allowed_dirs": list(self.allowed_dirs),
            "write_enabled": self.write_enabled,
            "version": self.version,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "pending": len(self._pending),
        }


class SessionRegistry:
    """Async-safe registry of active bridge sessions, one per user.

    Re-registration (reconnect) atomically replaces the previous session and
    fails all of its pending calls, so a stale socket can never answer for a
    fresh one.
    """

    def __init__(self, request_timeout: float = 18.0):
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()
        self.request_timeout = request_timeout

    async def register(self, session: BridgeSession) -> None:
        async with self._lock:
            old = self._sessions.get(session.user_id)
            if old is not None:
                logger.info(
                    "user %s re-registered (old client_id=%s -> new %s)",
                    session.user_id, old.client_id, session.client_id,
                )
                old.fail_pending(BridgeProtocolError(CODE_NO_BRIDGE, "session replaced by reconnect"))
                with contextlib.suppress(Exception):
                    await old.websocket.close(code=4000, reason="replaced by reconnect")
            self._sessions[session.user_id] = session
            logger.info("bridge registered: user=%s client_id=%s host=%s dirs=%s write=%s",
                        session.user_id, session.client_id, session.hostname,
                        session.allowed_dirs, session.write_enabled)

    async def unregister(self, user_id: str, session: BridgeSession) -> None:
        """Remove a session, but only if it is still the registered one
        (guards against a late disconnect from an already-replaced socket)."""
        async with self._lock:
            cur = self._sessions.get(user_id)
            if cur is session:
                del self._sessions[user_id]
                logger.info("bridge unregistered: user=%s client_id=%s", user_id, session.client_id)
        session.fail_pending(BridgeProtocolError(CODE_NO_BRIDGE, "bridge disconnected"))

    def get(self, user_id: str) -> BridgeSession | None:
        return self._sessions.get(user_id)

    def require(self, user_id: str) -> BridgeSession:
        sess = self._sessions.get(user_id)
        if sess is None:
            raise BridgeProtocolError(
                CODE_NO_BRIDGE,
                "no bridge client is connected for this user; start the local bridge client",
            )
        return sess

    def snapshot(self) -> list[dict[str, Any]]:
        return [s.info() for s in self._sessions.values()]

    async def call(
        self,
        user_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send {id, method, params} to the user's bridge and await its reply.

        Raises BridgeProtocolError on: no session, timeout, transport failure,
        or an error object returned by the client. Returns the client's
        `result` payload on success.
        """
        sess = self.require(user_id)
        timeout = timeout or self.request_timeout
        req_id = sess.next_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        sess._pending[req_id] = fut

        envelope = {"id": req_id, "method": method, "params": params or {}}
        try:
            await sess.websocket.send_json(envelope)
        except Exception as exc:  # transport dead
            sess._pending.pop(req_id, None)
            raise BridgeProtocolError(CODE_NO_BRIDGE, f"bridge transport failed: {exc}") from exc

        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BridgeProtocolError(
                CODE_TIMEOUT, f"bridge did not answer {method} within {timeout:.0f}s"
            ) from exc
        finally:
            sess._pending.pop(req_id, None)

        sess.touch()
        if isinstance(reply, dict) and reply.get("error"):
            err = reply["error"]
            code = err.get("code", -32000)
            msg = err.get("message", "bridge error")
            raise BridgeProtocolError(int(code), str(msg))
        if isinstance(reply, dict):
            return reply.get("result")
        return reply

    def deliver(self, user_id: str, raw_id: Any, message: dict[str, Any]) -> bool:
        """Route an inbound WS frame to the waiting caller.

        Returns True if the frame matched a pending request. Called by the WS
        read loop; must not raise into it.
        """
        sess = self._sessions.get(user_id)
        if sess is None:
            return False
        try:
            req_id = int(raw_id)
        except (TypeError, ValueError):
            return False
        fut = sess._pending.get(req_id)
        if fut is None or fut.done():
            return False
        fut.set_result(message)
        return True
