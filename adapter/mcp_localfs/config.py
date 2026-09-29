"""Environment-driven configuration and the split-token auth model.

Tokens bind an Octop user to their bridge session. 任务书 v1.1 §七评审补遗
splits the former single token into two independent namespaces (see
tokens.py):

  * mcp_token    — Octop connector Bearer auth on /mcp/localfs/
  * bridge_token — client WSS auth on /mcp-localfs/ws

Each direction verifies only its own kind; either can be revoked on its own.
Only SHA-256 hashes are retained — plaintexts are shown exactly once by the
issuing admin endpoint.

Bootstrap (dev / single-tenant) via environment:
  * LOCALFS_TOKENS="userA:tokA,..."          seeds MCP tokens
  * LOCALFS_BRIDGE_TOKENS="userA:btokA,..."  seeds bridge tokens
    (when unset, LOCALFS_TOKENS also seeds bridge tokens so existing
    single-token deployments keep working until they rotate)

Everything is read once at startup into a Settings dataclass; tests build a
Settings directly and inject it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .errors import BridgeProtocolError, CODE_AUTH_FAILED
from .tokens import KIND_BRIDGE, KIND_MCP, TokenStore

DEFAULT_MAX_READ_BYTES = 20 * 1024 * 1024   # 20 MB
DEFAULT_MAX_WRITE_BYTES = 10 * 1024 * 1024  # 10 MB
# 超时预算（任务书 v1.1 §七）：客户端单工具执行 ≤15s、桥往返 ≤18s、
# 适配器整体响应 ≤20s。适配器侧的桥等待取 18s。
DEFAULT_BRIDGE_TIMEOUT = 18.0               # A→B request/response budget
DEFAULT_IDLE_TIMEOUT = 120.0                # drop a bridge with no traffic


@dataclass
class Settings:
    # --- auth ---
    # Split-token store: mcp_tokens (Octop Bearer) and bridge_tokens (client
    # WSS), held as SHA-256 hashes. Seeded from LOCALFS_TOKENS /
    # LOCALFS_BRIDGE_TOKENS at startup; extended at runtime via the admin
    # issuing endpoint.
    token_store: TokenStore = field(default_factory=TokenStore)
    # Legacy static map, kept only as the source the store is seeded from
    # (and for the admin-endpoint guard "__admin__").
    static_tokens: dict[str, str] = field(default_factory=dict)

    # --- behavior switches ---
    # Master switch for the reserved write tools. Default OFF (read-only).
    allow_write: bool = False
    # Per-user override: user ids allowed to write even when allow_write is
    # globally off (staged rollout: "先 1 人只读 → 放开写权限 → 全员").
    write_allowlist: set[str] = field(default_factory=set)
    # Admin kill-switch: when True, every tools/call is refused. Models the
    # product-layer "管理员可整体停用" control.
    disabled: bool = False

    # --- limits / timeouts ---
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES
    bridge_timeout: float = DEFAULT_BRIDGE_TIMEOUT
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT

    # --- logging ---
    audit_log_path: str = "logs/calls.jsonl"
    log_level: str = "INFO"

    # --- server ---
    host: str = "0.0.0.0"  # noqa: S104 — container bind, fronted by a proxy
    port: int = 8080
    # Public base path prefix, kept configurable for reverse-proxy mounting.
    mcp_path: str = "/mcp/localfs/"
    ws_path: str = "/mcp-localfs/ws"

    # ------------------------------------------------------------------ auth
    def authenticate_mcp(self, token: str | None) -> str:
        """Validate an Octop connector Bearer token (mcp_token) and return
        the user_id. A bridge_token never passes here."""
        return self.token_store.authenticate(KIND_MCP, token)

    def authenticate_bridge(self, token: str | None) -> str:
        """Validate a client WSS token (bridge_token) and return the
        user_id. An mcp_token never passes here."""
        return self.token_store.authenticate(KIND_BRIDGE, token)

    # ------------------------------------------------------- write decisions
    def user_can_write(self, user_id: str) -> bool:
        """Whether write tools are enabled for this user right now."""
        if self.disabled:
            return False
        return self.allow_write or user_id in self.write_allowlist


def load_settings(env: os._Environ | dict | None = None) -> Settings:
    """Build Settings from the environment (or an injected mapping for tests)."""
    e = env if env is not None else os.environ

    static: dict[str, str] = {}
    raw_tokens = e.get("LOCALFS_TOKENS", "")
    for pair in raw_tokens.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user_id, _, tok = pair.partition(":")
        user_id, tok = user_id.strip(), tok.strip()
        if user_id and tok:
            static[user_id] = tok

    bridge_static: dict[str, str] = {}
    raw_bridge = e.get("LOCALFS_BRIDGE_TOKENS", "")
    for pair in raw_bridge.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user_id, _, tok = pair.partition(":")
        user_id, tok = user_id.strip(), tok.strip()
        if user_id and tok:
            bridge_static[user_id] = tok

    store = TokenStore()
    for user_id, tok in static.items():
        store.seed(KIND_MCP, user_id, tok)
    if bridge_static:
        for user_id, tok in bridge_static.items():
            store.seed(KIND_BRIDGE, user_id, tok)
    else:
        # Back-compat: a deployment that only set LOCALFS_TOKENS keeps its
        # clients connected until tokens are rotated to the split scheme.
        for user_id, tok in static.items():
            store.seed(KIND_BRIDGE, user_id, tok)

    write_allow: set[str] = set()
    for u in e.get("LOCALFS_WRITE_ALLOWLIST", "").split(","):
        u = u.strip()
        if u:
            write_allow.add(u)

    return Settings(
        token_store=store,
        static_tokens=static,
        allow_write=_env_flag(e.get("LOCALFS_ALLOW_WRITE"), False),
        write_allowlist=write_allow,
        disabled=_env_flag(e.get("LOCALFS_DISABLED"), False),
        max_read_bytes=_int(e.get("LOCALFS_MAX_READ_BYTES"), DEFAULT_MAX_READ_BYTES),
        max_write_bytes=_int(e.get("LOCALFS_MAX_WRITE_BYTES"), DEFAULT_MAX_WRITE_BYTES),
        bridge_timeout=_float(e.get("LOCALFS_BRIDGE_TIMEOUT"), DEFAULT_BRIDGE_TIMEOUT),
        idle_timeout=_float(e.get("LOCALFS_IDLE_TIMEOUT"), DEFAULT_IDLE_TIMEOUT),
        audit_log_path=e.get("LOCALFS_AUDIT_LOG", "logs/calls.jsonl"),
        log_level=e.get("LOCALFS_LOG_LEVEL", "INFO"),
        host=e.get("LOCALFS_HOST", "0.0.0.0"),
        port=_int(e.get("LOCALFS_PORT"), 8080),
        mcp_path=e.get("LOCALFS_MCP_PATH", "/mcp/localfs/"),
        ws_path=e.get("LOCALFS_WS_PATH", "/mcp-localfs/ws"),
    )


def _env_flag(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default
