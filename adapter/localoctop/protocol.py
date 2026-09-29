"""MCP protocol definitions: JSON-RPC envelopes, the MCP tool catalog, and
the bridge (A→B) method names.

Two layers live here:

  * MCP layer (B→Octop): the streamable_http JSON-RPC methods `initialize`,
    `tools/list`, `tools/call`, and the tool schemas advertised to Octop.
  * Bridge layer (A→B): the `{id, method, params}` frames forwarded to the Go
    client over WSS. Method names match the MCP tool names 1:1.

The tool set mirrors the official MCP filesystem server: four read-only tools
plus two reserved write tools that stay hidden unless write access is enabled.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import (
    BridgeProtocolError,
    CODE_INVALID_PARAMS,
    CODE_METHOD_NOT_FOUND,
    CODE_PARSE_ERROR,
)

# Protocol identity reported during the MCP handshake.
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "localoctop"
SERVER_VERSION = "1.0.0"

# Bridge method names — kept identical to localoctop/protocol.go.
M_LIST_DIRECTORY = "list_directory"
M_READ_FILE = "read_file"
M_SEARCH_FILES = "search_files"
M_GET_FILE_INFO = "get_file_info"
M_WRITE_FILE = "write_file"
M_CREATE_DIRECTORY = "create_directory"

READ_ONLY_TOOLS = (
    M_LIST_DIRECTORY,
    M_READ_FILE,
    M_SEARCH_FILES,
    M_GET_FILE_INFO,
)
WRITE_TOOLS = (M_WRITE_FILE, M_CREATE_DIRECTORY)
ALL_TOOLS = READ_ONLY_TOOLS + WRITE_TOOLS


# ---------------------------------------------------------------------------
# MCP tool catalog (inputSchema per the MCP tools spec)
# ---------------------------------------------------------------------------

def _path_prop(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    M_LIST_DIRECTORY: {
        "name": M_LIST_DIRECTORY,
        "description": "List the files and sub-directories of a whitelisted directory on the employee's machine. "
                       "Returns name/type/size/mtime for each entry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop(
                    "Directory path, relative to the whitelist root (e.g. \".\" or \"docs\"). "
                    "Absolute paths are accepted only if inside the whitelist. Defaults to the root."
                ),
            },
        },
    },
    M_READ_FILE: {
        "name": M_READ_FILE,
        "description": "Read a text (or small binary) file from the employee's whitelisted directory. "
                       "Text is returned inline; binary is base64-encoded. Enforces a per-file size cap.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("File path relative to the whitelist root, or absolute inside it."),
            },
            "required": ["path"],
        },
    },
    M_SEARCH_FILES: {
        "name": M_SEARCH_FILES,
        "description": "Search a whitelisted directory tree by filename glob (e.g. \"*.md\") or substring. "
                       "Optionally match file contents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob (\"*.md\") or substring to match against names."},
                "path": _path_prop("Directory to search within. Defaults to the whitelist root."),
                "max_results": {"type": "integer", "description": "Cap on returned matches (default 1000).", "minimum": 1},
                "match_content": {"type": "boolean", "description": "If true, also match file text content (default false)."},
                "include_dirs": {"type": "boolean", "description": "Include directories in results (default true)."},
            },
            "required": ["pattern"],
        },
    },
    M_GET_FILE_INFO: {
        "name": M_GET_FILE_INFO,
        "description": "Get metadata (size, type, mtime, read-only, symlink, mime) for a whitelisted path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("File or directory path relative to the whitelist root, or absolute inside it."),
            },
            "required": ["path"],
        },
    },
    M_WRITE_FILE: {
        "name": M_WRITE_FILE,
        "description": "RESERVED (write access off by default). Write text or base64 content to a whitelisted path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Destination file path relative to the whitelist root, or absolute inside it."),
                "content": {"type": "string", "description": "File content (raw text, or base64 when encoding=base64)."},
                "encoding": {"type": "string", "enum": ["utf-8", "base64"], "description": "Content encoding (default utf-8)."},
            },
            "required": ["path", "content"],
        },
    },
    M_CREATE_DIRECTORY: {
        "name": M_CREATE_DIRECTORY,
        "description": "RESERVED (write access off by default). Create a directory under a whitelisted path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Directory path to create, relative to the whitelist root, or absolute inside it."),
            },
            "required": ["path"],
        },
    },
}


def tools_list(write_enabled: bool) -> list[dict[str, Any]]:
    """Return the tool descriptors advertised to Octop.

    Write tools are included only when write access is globally enabled; with
    the default configuration exactly the four read-only tools are returned,
    matching acceptance criterion #1.
    """
    names = ALL_TOOLS if write_enabled else READ_ONLY_TOOLS
    return [TOOL_SCHEMAS[n] for n in names]


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers
# ---------------------------------------------------------------------------

def parse_request(raw: bytes | str) -> dict[str, Any]:
    """Parse and minimally validate an inbound JSON-RPC request object."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BridgeProtocolError(CODE_PARSE_ERROR, f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise BridgeProtocolError(CODE_PARSE_ERROR, "request must be a JSON object")
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise BridgeProtocolError(CODE_INVALID_PARAMS, "missing or invalid 'method'")
    return obj


def jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def is_notification(obj: dict[str, Any]) -> bool:
    """A JSON-RPC notification has no 'id' field; it expects no response."""
    return "id" not in obj


def validate_tool_name(name: str, write_enabled: bool) -> None:
    """Raise CODE_METHOD_NOT_FOUND for unknown or write-disabled tools."""
    if name not in ALL_TOOLS:
        raise BridgeProtocolError(CODE_METHOD_NOT_FOUND, f"unknown tool: {name}")
    if name in WRITE_TOOLS and not write_enabled:
        # Unknown to the client as far as Octop is concerned when writes are off.
        raise BridgeProtocolError(CODE_METHOD_NOT_FOUND, f"tool not enabled: {name}")
