"""Tests for Settings: env loading, split-token auth (mcp vs bridge),
independent revocation, and write gating.
"""

from __future__ import annotations

import pytest

from localoctop.config import Settings, load_settings
from localoctop.errors import BridgeProtocolError, CODE_AUTH_FAILED
from localoctop.tokens import KIND_BRIDGE, KIND_MCP, TokenStore


def _store() -> TokenStore:
    store = TokenStore()
    store.seed(KIND_MCP, "alice", "tok-a")
    store.seed(KIND_BRIDGE, "alice", "btok-a")
    return store


def test_load_settings_parses_tokens_and_switches():
    env = {
        "LOCALOCTOP_MCP_TOKENS": "alice:tok-a, bob:tok-b ,brokenpair, :empty",
        "LOCALOCTOP_CLIENT_TOKENS": "alice:btok-a, bob:btok-b",
        "LOCALOCTOP_ALLOW_WRITE": "true",
        "LOCALOCTOP_WRITE_ALLOWLIST": "alice,carol",
        "LOCALOCTOP_MAX_READ_BYTES": "1048576",
        "LOCALOCTOP_BRIDGE_TIMEOUT": "12.5",
        "LOCALOCTOP_PORT": "9999",
        "LOCALOCTOP_LOG_LEVEL": "debug",
    }
    s = load_settings(env)
    assert s.static_tokens == {"alice": "tok-a", "bob": "tok-b"}
    assert s.allow_write is True
    assert s.write_allowlist == {"alice", "carol"}
    assert s.max_read_bytes == 1048576
    assert s.bridge_timeout == 12.5
    assert s.port == 9999
    assert s.log_level == "debug"
    # Both namespaces seeded from their own env vars.
    assert s.authenticate_mcp("tok-a") == "alice"
    assert s.authenticate_bridge("btok-a") == "alice"


def test_load_settings_defaults_are_safe():
    s = load_settings({})
    assert s.allow_write is False, "write must default OFF"
    assert s.disabled is False
    assert s.max_read_bytes == 20 * 1024 * 1024
    assert s.max_write_bytes == 10 * 1024 * 1024
    # 任务书 v1.1 §七 timeout budget: bridge round-trip ≤18s.
    assert s.bridge_timeout == 18.0
    assert s.mcp_path == "/mcp/localoctop/"
    assert s.ws_path == "/mcp/localoctop/ws"


def test_load_settings_bad_numbers_fall_back():
    s = load_settings({"LOCALOCTOP_MAX_READ_BYTES": "abc", "LOCALOCTOP_PORT": "xyz"})
    assert s.max_read_bytes == 20 * 1024 * 1024
    assert s.port == 8080


def test_load_settings_back_compat_single_token_seeds_both():
    """A deployment that only set LOCALOCTOP_MCP_TOKENS keeps working on both
    directions until it rotates to the split scheme."""
    s = load_settings({"LOCALOCTOP_MCP_TOKENS": "alice:tok-a"})
    assert s.authenticate_mcp("tok-a") == "alice"
    assert s.authenticate_bridge("tok-a") == "alice"


def test_authenticate_mcp_token():
    s = Settings(token_store=_store())
    assert s.authenticate_mcp("tok-a") == "alice"
    with pytest.raises(BridgeProtocolError) as ei:
        s.authenticate_mcp("tok-wrong")
    assert ei.value.code == CODE_AUTH_FAILED
    with pytest.raises(BridgeProtocolError):
        s.authenticate_mcp(None)
    with pytest.raises(BridgeProtocolError):
        s.authenticate_mcp("")


def test_authenticate_bridge_token():
    s = Settings(token_store=_store())
    assert s.authenticate_bridge("btok-a") == "alice"
    with pytest.raises(BridgeProtocolError) as ei:
        s.authenticate_bridge("btok-wrong")
    assert ei.value.code == CODE_AUTH_FAILED


def test_token_directions_are_mutually_exclusive():
    """An mcp_token must not pass bridge auth and vice versa — even though
    both belong to the same user."""
    s = Settings(token_store=_store())
    with pytest.raises(BridgeProtocolError):
        s.authenticate_bridge("tok-a")   # mcp token on the WSS side
    with pytest.raises(BridgeProtocolError):
        s.authenticate_mcp("btok-a")     # bridge token on the MCP side


def test_issue_token_shows_plaintext_once_and_stores_hash():
    store = TokenStore()
    token = store.issue(KIND_MCP, "carol")
    assert token.startswith("lfs-mcp-")
    assert store.authenticate(KIND_MCP, token) == "carol"
    # The store never exposes the plaintext again: only a short hint.
    listing = store.list_user(KIND_MCP, "carol")
    assert len(listing) == 1
    assert token not in str(listing)
    assert listing[0]["hint"].startswith("lfs-mcp-")


def test_independent_revocation():
    """Revoking one direction leaves the other live."""
    store = TokenStore()
    mcp_tok = store.issue(KIND_MCP, "dave")
    bridge_tok = store.issue(KIND_BRIDGE, "dave")

    assert store.revoke_user(KIND_BRIDGE, "dave") == 1
    with pytest.raises(BridgeProtocolError):
        store.authenticate(KIND_BRIDGE, bridge_tok)
    # MCP side untouched.
    assert store.authenticate(KIND_MCP, mcp_tok) == "dave"

    assert store.revoke_user(KIND_MCP, "dave") == 1
    with pytest.raises(BridgeProtocolError):
        store.authenticate(KIND_MCP, mcp_tok)


def test_revoke_token_single():
    store = TokenStore()
    t1 = store.issue(KIND_MCP, "erin")
    t2 = store.issue(KIND_MCP, "erin")
    assert store.revoke_token(KIND_MCP, t1) is True
    assert store.revoke_token(KIND_MCP, t1) is False  # already revoked
    with pytest.raises(BridgeProtocolError):
        store.authenticate(KIND_MCP, t1)
    assert store.authenticate(KIND_MCP, t2) == "erin"


def test_user_can_write_gating():
    s = Settings(allow_write=False, write_allowlist={"carol"})
    assert s.user_can_write("alice") is False
    assert s.user_can_write("carol") is True
    s.allow_write = True
    assert s.user_can_write("alice") is True
    s.disabled = True  # admin kill-switch beats everything
    assert s.user_can_write("alice") is False
    assert s.user_can_write("carol") is False


def test_static_token_lookup_is_not_prefix_confusable():
    """Token 'tok-a' must not authenticate via a startswith-style bug."""
    s = Settings(token_store=_store())
    with pytest.raises(BridgeProtocolError):
        s.authenticate_mcp("tok-abc")
    with pytest.raises(BridgeProtocolError):
        s.authenticate_mcp("tok-")
