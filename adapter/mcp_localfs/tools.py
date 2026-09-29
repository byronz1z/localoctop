"""MCP tools/call handling: validation, fan-out to the bridge, and rendering
of bridge results into MCP content blocks.

Octop speaks MCP; the bridge speaks the plain {id, method, params} protocol.
This module is the translation layer, including the security checks the server
owns: per-user session isolation (enforced by SessionRegistry keying),
the admin kill-switch, write-tool gating, and payload size sanity checks on
results coming back from the client (never trust the far end).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from typing import Any

from .audit import AuditLog
from .config import Settings
from .errors import (
    BridgeProtocolError,
    CODE_INTERNAL,
    CODE_INVALID_PARAMS,
    CODE_NOT_ALLOWED,
    CODE_TOO_LARGE,
)
from .protocol import (
    M_CREATE_DIRECTORY,
    M_GET_FILE_INFO,
    M_LIST_DIRECTORY,
    M_READ_FILE,
    M_SEARCH_FILES,
    M_WRITE_FILE,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    validate_tool_name,
)
from .sessions import SessionRegistry

logger = logging.getLogger("mcp_localfs.tools")

# Hard ceiling on a rendered MCP text block, independent of client config, so
# a compromised/buggy client cannot make us relay arbitrarily large payloads.
SERVER_MAX_RESULT_BYTES = 24 * 1024 * 1024

# 任务书 v1.1 §七 timeout budget: the adapter's overall response to one
# tools/call must land within 20s. The bridge round-trip itself is capped at
# settings.bridge_timeout (≤18s); this outer bound also covers validation,
# rendering and audit overhead, so the connector never waits past the budget.
ADAPTER_RESPONSE_BUDGET = 20.0


class ToolService:
    """Executes MCP tool calls against a user's bridge session."""

    def __init__(self, settings: Settings, registry: SessionRegistry, audit: AuditLog):
        self.settings = settings
        self.registry = registry
        self.audit = audit

    async def call_tool(self, user_id: str, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """Run one tools/call. Returns the MCP result object
        ({"content": [...], "isError": bool}).

        Raises BridgeProtocolError for auth/routing/protocol problems; tool-
        level failures (denied path, missing file, ...) are returned as
        isError results, per the MCP spec (they are not protocol errors).
        """
        started = time.monotonic()
        arguments = arguments if isinstance(arguments, dict) else {}
        req_path = str(arguments.get("path", "") or arguments.get("pattern", ""))[:512]

        # --- server-side gates -------------------------------------------
        if self.settings.disabled:
            await self._audit(user_id, name, req_path, "deny", CODE_NOT_ALLOWED,
                              "connector disabled by administrator", started)
            raise BridgeProtocolError(CODE_NOT_ALLOWED, "localfs connector is disabled by the administrator")

        write_enabled = self.settings.user_can_write(user_id)
        try:
            validate_tool_name(name, write_enabled)
        except BridgeProtocolError as exc:
            await self._audit(user_id, name, req_path, "deny", exc.code, exc.message, started)
            raise

        # Extra gate: even when the server allows writes, the client session
        # must also have registered with write support (both switches matter).
        session = self.registry.require(user_id)
        if name in WRITE_TOOLS and not session.write_enabled:
            await self._audit(user_id, name, req_path, "deny", CODE_NOT_ALLOWED,
                              "client session has writes disabled", started, session.client_id)
            return self._error_result(f"the connected client does not allow {name} (write switch off on the machine)")

        # Payload size sanity for writes before they even hit the wire.
        if name == M_WRITE_FILE:
            content = arguments.get("content")
            if not isinstance(content, str):
                raise BridgeProtocolError(CODE_INVALID_PARAMS, "write_file requires string 'content'")
            approx = len(content)
            limit = self.settings.max_write_bytes * 2  # base64 inflates ~4/3
            if approx > limit:
                await self._audit(user_id, name, req_path, "deny", CODE_TOO_LARGE,
                                  f"content {approx}B exceeds limit", started, session.client_id)
                return self._error_result(f"content exceeds the {self.settings.max_write_bytes}-byte write limit")

        # --- fan out to the bridge ----------------------------------------
        try:
            result = await self.registry.call(user_id, name, arguments, timeout=self.settings.bridge_timeout)
        except BridgeProtocolError as exc:
            decision = "error" if exc.code == CODE_INTERNAL else "deny"
            await self._audit(user_id, name, req_path, decision, exc.code, exc.message,
                              started, session.client_id)
            # Surface client-side denials as isError content (tool failure),
            # and routing/protocol failures as MCP protocol errors.
            if exc.code in (CODE_INTERNAL,):
                raise
            return self._error_result(exc.message)

        rendered = self._render(name, result)
        await self._audit(user_id, name, req_path, "allow", 0, "", started, session.client_id)
        return rendered

    # --- rendering ---------------------------------------------------------

    def _render(self, name: str, result: Any) -> dict[str, Any]:
        """Convert a bridge result payload into MCP content blocks."""
        if not isinstance(result, dict):
            raise BridgeProtocolError(CODE_INTERNAL, f"bridge returned non-object result for {name}")

        if name == M_READ_FILE:
            return self._render_read(result)
        if name in (M_LIST_DIRECTORY, M_SEARCH_FILES, M_GET_FILE_INFO, M_WRITE_FILE, M_CREATE_DIRECTORY):
            return self._render_json(result)
        return self._render_json(result)

    def _render_read(self, result: dict[str, Any]) -> dict[str, Any]:
        encoding = str(result.get("encoding", "utf-8"))
        if encoding == "base64":
            b64 = result.get("base64", "")
            if not isinstance(b64, str):
                raise BridgeProtocolError(CODE_INTERNAL, "bad base64 field from bridge")
            try:
                raw = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise BridgeProtocolError(CODE_INTERNAL, f"bridge sent invalid base64: {exc}") from exc
            if len(raw) > SERVER_MAX_RESULT_BYTES:
                raise BridgeProtocolError(CODE_TOO_LARGE, "bridge result exceeds server size ceiling")
            return {
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "file:///" + str(result.get("path", "blob")).lstrip("/"),
                            "mimeType": "application/octet-stream",
                            "blob": b64,
                        },
                    }
                ],
                "isError": False,
            }
        text = result.get("text", "")
        if not isinstance(text, str):
            raise BridgeProtocolError(CODE_INTERNAL, "bad text field from bridge")
        if len(text.encode("utf-8", "replace")) > SERVER_MAX_RESULT_BYTES:
            raise BridgeProtocolError(CODE_TOO_LARGE, "bridge result exceeds server size ceiling")
        header = f"== {result.get('path', '')} ({result.get('size', len(text))} bytes) ==\n"
        return {"content": [{"type": "text", "text": header + text}], "isError": False}

    def _render_json(self, result: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(result, ensure_ascii=False, indent=1)
        if len(text.encode("utf-8", "replace")) > SERVER_MAX_RESULT_BYTES:
            raise BridgeProtocolError(CODE_TOO_LARGE, "bridge result exceeds server size ceiling")
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _error_result(self, message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": True}

    async def _audit(self, user_id: str, method: str, path: str, decision: str,
                     code: int, detail: str, started: float, client_id: str = "") -> None:
        await self.audit.record(
            user_id=user_id,
            method=method,
            path=path,
            decision=decision,
            code=code,
            detail=detail,
            duration_ms=(time.monotonic() - started) * 1000,
            client_id=client_id,
        )


__all__ = [
    "ToolService",
    "SERVER_MAX_RESULT_BYTES",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
]
