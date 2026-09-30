"""MCP protocol definitions: JSON-RPC envelopes, the MCP tool catalog, and
the bridge (A→B) method names.

Two layers live here:

  * MCP layer (B→Octop): the streamable_http JSON-RPC methods `initialize`,
    `tools/list`, `tools/call`, and the tool schemas advertised to Octop.
  * Bridge layer (A→B): the `{id, method, params}` frames forwarded to the Go
    client over WSS. Method names match the MCP tool names 1:1.

v0.5.0: the catalog covers the full official MCP filesystem server tool set
(read/list/tree/info/media/multi-read/search/edit/write/create/move/delete/
remove-dir/allowed-dirs) plus our zip/unzip extensions. Tool annotations
follow the MCP 2025-06-18 `annotations` object (readOnlyHint / destructiveHint
/ idempotentHint / openWorldHint:false — everything is confined to the
whitelist). Write-class tools appear only when write access is enabled on
both server and client (user ruling: switch kept, default ON).
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
SERVER_VERSION = "1.3.0"

# Bridge method names — kept identical to localoctop/protocol.go.
M_LIST_DIRECTORY = "list_directory"
M_READ_FILE = "read_file"
M_SEARCH_FILES = "search_files"
M_GET_FILE_INFO = "get_file_info"
M_WRITE_FILE = "write_file"
M_CREATE_DIRECTORY = "create_directory"
M_READ_MEDIA_FILE = "read_media_file"
M_READ_MULTIPLE_FILES = "read_multiple_files"
M_EDIT_FILE = "edit_file"
M_LIST_DIRECTORY_WITH_SIZES = "list_directory_with_sizes"
M_DIRECTORY_TREE = "directory_tree"
M_MOVE_FILE = "move_file"
M_DELETE_FILE = "delete_file"
M_REMOVE_DIRECTORY = "remove_directory"
M_ZIP_FILES = "zip_files"
M_UNZIP_FILE = "unzip_file"
M_LIST_ALLOWED_DIRECTORIES = "list_allowed_directories"

READ_ONLY_TOOLS = (
    M_LIST_ALLOWED_DIRECTORIES,
    M_LIST_DIRECTORY,
    M_LIST_DIRECTORY_WITH_SIZES,
    M_DIRECTORY_TREE,
    M_SEARCH_FILES,
    M_READ_FILE,
    M_READ_MEDIA_FILE,
    M_READ_MULTIPLE_FILES,
    M_GET_FILE_INFO,
    M_ZIP_FILES,
)
WRITE_TOOLS = (
    M_CREATE_DIRECTORY,
    M_WRITE_FILE,
    M_EDIT_FILE,
    M_MOVE_FILE,
    M_DELETE_FILE,
    M_REMOVE_DIRECTORY,
    M_UNZIP_FILE,
)
ALL_TOOLS = READ_ONLY_TOOLS + WRITE_TOOLS


def _ann(title: str, read_only: bool = False, destructive: bool = False,
         idempotent: bool = True) -> dict[str, Any]:
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": False,
    }


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
                "depth": {
                    "type": "integer",
                    "description": "Directory levels to include (1 = immediate children only). Default 1, max 3.",
                    "minimum": 1, "maximum": 3, "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Cap on returned entries across all levels. Default 500, max 2000.",
                    "minimum": 1, "maximum": 2000, "default": 500,
                },
            },
        },
        "annotations": _ann("List directory", read_only=True),
    },
    M_READ_FILE: {
        "name": M_READ_FILE,
        "description": "Read a text (or small binary) file from the employee's whitelisted directory. "
                       "Text is returned inline; binary is base64-encoded. Supports byte-window reads "
                       "(offset/length); a whole-file read enforces a per-file size cap.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("File path relative to the whitelist root, or absolute inside it."),
                "offset": {
                    "type": "integer",
                    "description": "Byte offset to start reading from (default 0).",
                    "minimum": 0, "default": 0,
                },
                "length": {
                    "type": "integer",
                    "description": "Bytes to read from offset; 0/omitted = whole file (capped at 20 MB).",
                    "minimum": 0, "default": 0,
                },
            },
            "required": ["path"],
        },
        "annotations": _ann("Read file", read_only=True),
    },
    M_SEARCH_FILES: {
        "name": M_SEARCH_FILES,
        "description": "Search a whitelisted directory tree by filename glob/substring, or by regular "
                       "expression (mode=\"regex\", optionally match_content for line-level content hits "
                       "with line numbers and previews). Skips .git/node_modules subtrees by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob (\"*.md\"), substring, or (mode=regex) regular expression."},
                "path": _path_prop("Directory to search within. Defaults to the whitelist root."),
                "mode": {"type": "string", "enum": ["glob", "regex"], "description": "Matching mode (default glob)."},
                "excludePatterns": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Name globs/substrings to exclude (dirs and files).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Cap on returned matches (default 200, max 1000).",
                    "minimum": 1, "maximum": 1000, "default": 200,
                },
                "max_results": {"type": "integer", "description": "Deprecated alias of limit.", "minimum": 1},
                "match_content": {"type": "boolean", "description": "If true, also match file text content (default false)."},
                "include_dirs": {"type": "boolean", "description": "Include directories in results (default true)."},
                "recursive": {"type": "boolean", "description": "Walk sub-directories (default true)."},
            },
            "required": ["pattern"],
        },
        "annotations": _ann("Search files", read_only=True),
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
        "annotations": _ann("Get file info", read_only=True),
    },
    M_READ_MEDIA_FILE: {
        "name": M_READ_MEDIA_FILE,
        "description": "Read a media/binary file (image, audio, or any file) and return it base64-encoded with "
                       "the guessed MIME type. One-shot cap 30 MB; larger files use zip_files or windowed read_file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("File path relative to the whitelist root, or absolute inside it."),
            },
            "required": ["path"],
        },
        "annotations": _ann("Read media file", read_only=True),
    },
    M_READ_MULTIPLE_FILES: {
        "name": M_READ_MULTIPLE_FILES,
        "description": "Read several text files in one call. Individual failures are reported per file "
                       "without aborting the batch (official semantics). Batch cap 50 files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Whitelist-relative file paths to read (max 50).",
                },
            },
            "required": ["paths"],
        },
        "annotations": _ann("Read multiple files", read_only=True),
    },
    M_LIST_DIRECTORY_WITH_SIZES: {
        "name": M_LIST_DIRECTORY_WITH_SIZES,
        "description": "List a whitelisted directory with per-entry sizes and aggregate totals; sortable by "
                       "name or size (files first, largest first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Directory to list. Defaults to the whitelist root."),
                "sortBy": {"type": "string", "enum": ["name", "size"], "description": "Sort order (default name)."},
            },
        },
        "annotations": _ann("List directory with sizes", read_only=True),
    },
    M_DIRECTORY_TREE: {
        "name": M_DIRECTORY_TREE,
        "description": "Recursively build a directory tree (name/type/children) for a whitelisted path, with "
                       "optional exclude patterns. Node cap 20000; truncated flag when hit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Directory root for the tree. Defaults to the whitelist root."),
                "excludePatterns": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Name globs/substrings to skip (e.g. \"node_modules\", \"*.tmp\").",
                },
            },
        },
        "annotations": _ann("Directory tree", read_only=True),
    },
    M_ZIP_FILES: {
        "name": M_ZIP_FILES,
        "description": "Bundle whitelisted files/directories into a zip archive built in memory and returned "
                       "base64-encoded (nothing written to disk). Skips .git/node_modules; excludePatterns honoured. "
                       "Archive cap 30 MB, member cap 5000.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Whitelist files/directories to include.",
                },
                "excludePatterns": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Name globs/substrings to skip inside directory walks.",
                },
            },
            "required": ["paths"],
        },
        "annotations": _ann("Zip files", read_only=True),
    },
    M_UNZIP_FILE: {
        "name": M_UNZIP_FILE,
        "description": "Extract a zip archive that lives inside the whitelist into a whitelisted destination "
                       "directory. Zip-slip and zip-bomb defences enforced; existing members need overwrite=true. "
                       "Requires write access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "archive": _path_prop("Zip archive path (whitelist-relative or absolute inside)."),
                "dest": _path_prop("Destination directory inside the whitelist."),
                "overwrite": {"type": "boolean", "description": "Overwrite existing members (default false)."},
            },
            "required": ["archive", "dest"],
        },
        "annotations": _ann("Unzip file", idempotent=False),
    },
    M_LIST_ALLOWED_DIRECTORIES: {
        "name": M_LIST_ALLOWED_DIRECTORIES,
        "description": "List the directories this server is allowed to access — the whitelist roots. "
                       "Everything outside them is refused.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": _ann("List allowed directories", read_only=True),
    },
    M_WRITE_FILE: {
        "name": M_WRITE_FILE,
        "description": "Write text or base64 content to a whitelisted path on the employee's machine. "
                       "Requires write access enabled on both server and client.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Destination file path relative to the whitelist root, or absolute inside it."),
                "content": {"type": "string", "description": "File content (raw text, or base64 when encoding=base64)."},
                "encoding": {"type": "string", "enum": ["utf-8", "base64"], "description": "Content encoding (default utf-8)."},
            },
            "required": ["path", "content"],
        },
        "annotations": _ann("Write file"),
    },
    M_CREATE_DIRECTORY: {
        "name": M_CREATE_DIRECTORY,
        "description": "Create a directory under a whitelisted path on the employee's machine. "
                       "Requires write access enabled on both server and client.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Directory path to create, relative to the whitelist root, or absolute inside it."),
            },
            "required": ["path"],
        },
        "annotations": _ann("Create directory"),
    },
    M_EDIT_FILE: {
        "name": M_EDIT_FILE,
        "description": "Apply targeted text edits to a UTF-8 whitelisted file: each {oldText,newText} pair must "
                       "match exactly one location (ambiguous or missing matches are errors). dryRun=true returns "
                       "the unified diff without writing. Atomic tmp+rename replace. Requires write access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("File to edit (whitelist-relative or absolute inside)."),
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {"type": "string", "description": "Exact text to find (must be unique in the file)."},
                            "newText": {"type": "string", "description": "Replacement text ('' deletes)."},
                        },
                        "required": ["oldText", "newText"],
                    },
                },
                "dryRun": {"type": "boolean", "description": "Preview the diff without writing (default false)."},
            },
            "required": ["path", "edits"],
        },
        "annotations": _ann("Edit file", idempotent=False),
    },
    M_MOVE_FILE: {
        "name": M_MOVE_FILE,
        "description": "Move or rename a file/directory inside the whitelist. Never overwrites: an existing "
                       "destination is refused. Moving a directory into its own subtree is refused. "
                       "Requires write access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": _path_prop("Existing path to move."),
                "destination": _path_prop("New path; must not already exist."),
            },
            "required": ["source", "destination"],
        },
        "annotations": _ann("Move file", destructive=True, idempotent=False),
    },
    M_DELETE_FILE: {
        "name": M_DELETE_FILE,
        "description": "Delete a whitelisted FILE. Default moves it to the Windows recycle bin (recoverable); "
                       "permanent=true hard-deletes. Platforms without a recycle bin refuse unless permanent=true. "
                       "Requires write access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("File to delete (directories need remove_directory)."),
                "permanent": {"type": "boolean", "description": "Skip the recycle bin and delete irrevocably (default false)."},
            },
            "required": ["path"],
        },
        "annotations": _ann("Delete file", destructive=True, idempotent=False),
    },
    M_REMOVE_DIRECTORY: {
        "name": M_REMOVE_DIRECTORY,
        "description": "Remove a whitelisted DIRECTORY. Non-empty directories require recursive=true; the "
                       "whitelist root itself is always refused. Recycle-bin default like delete_file; "
                       "permanent=true hard-deletes. Requires write access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _path_prop("Directory to remove (files need delete_file)."),
                "recursive": {"type": "boolean", "description": "Remove non-empty contents too (default false)."},
                "permanent": {"type": "boolean", "description": "Skip the recycle bin (default false)."},
            },
            "required": ["path"],
        },
        "annotations": _ann("Remove directory", destructive=True, idempotent=False),
    },
}

def tools_list(write_enabled: bool) -> list[dict[str, Any]]:
    """Return the tool descriptors advertised to Octop.

    Write tools appear only when write access is enabled on the server side;
    the client's own switch is enforced per-call in tools.py. With writes off
    the catalog is exactly the read-only set (acceptance criterion #1).
    READ_ONLY_TOOLS / WRITE_TOOLS are the authoritative catalog order.
    """
    names = list(READ_ONLY_TOOLS)
    if write_enabled:
        names += list(WRITE_TOOLS)
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
