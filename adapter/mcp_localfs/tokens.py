"""Split bearer tokens: one namespace for the MCP side, one for the bridge.

任务书 v1.1 §七评审补遗: the single shared token is replaced by two
independent token kinds:

  * **mcp_token**    — presented by Octop's connector as `Authorization:
                       Bearer` on the /mcp/localfs/ endpoint.
  * **bridge_token** — presented by the employee's client on the outbound
                       WSS bridge (?token= or Authorization header).

The two namespaces are fully separate: an mcp_token never authenticates a
bridge connection and a bridge_token never passes the MCP Bearer check, and
each kind can be revoked independently per user.

Only SHA-256 hashes are kept in memory/on disk — the plaintext is returned
exactly once, from the issuing admin endpoint, and never stored.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field

from .errors import BridgeProtocolError, CODE_AUTH_FAILED

KIND_MCP = "mcp"
KIND_BRIDGE = "bridge"
_KINDS = (KIND_MCP, KIND_BRIDGE)

# Plaintext prefixes make the kind obvious at a glance in configs/logs and
# rule out "is this the WSS token or the connector token?" confusion.
_PREFIX = {KIND_MCP: "lfs-mcp-", KIND_BRIDGE: "lfs-bridge-"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class TokenRecord:
    user_id: str
    created_at: float
    revoked: bool = False
    # First characters of the plaintext, so ops can tell tokens apart in
    # listings without ever storing the secret itself.
    hint: str = ""


@dataclass
class TokenStore:
    """Holds both token namespaces, keyed by SHA-256 hash of the plaintext."""

    _mcp: dict[str, TokenRecord] = field(default_factory=dict)
    _bridge: dict[str, TokenRecord] = field(default_factory=dict)

    def _table(self, kind: str) -> dict[str, TokenRecord]:
        if kind == KIND_MCP:
            return self._mcp
        if kind == KIND_BRIDGE:
            return self._bridge
        raise ValueError(f"unknown token kind: {kind}")

    # -------------------------------------------------------------- issuing
    def issue(self, kind: str, user_id: str) -> str:
        """Mint a fresh token. Returns the plaintext — the only time it is
        ever available; only its hash is kept."""
        token = _PREFIX[kind] + secrets.token_hex(24)
        self._table(kind)[_hash(token)] = TokenRecord(
            user_id=user_id,
            created_at=time.time(),
            hint=token[: len(_PREFIX[kind]) + 8],
        )
        return token

    def seed(self, kind: str, user_id: str, plaintext: str) -> None:
        """Register a pre-existing plaintext (dev/test bootstrap from env
        vars). Production deployments issue via the admin endpoint instead."""
        if not plaintext:
            return
        self._table(kind)[_hash(plaintext)] = TokenRecord(
            user_id=user_id,
            created_at=time.time(),
            hint=plaintext[:16],
        )

    # ---------------------------------------------------------- verification
    def authenticate(self, kind: str, token: str | None) -> str:
        """Return the user_id behind a valid token of `kind`, or raise
        CODE_AUTH_FAILED. Tokens of the *other* kind are just unknown here —
        this is what makes the two directions mutually exclusive."""
        if not token:
            raise BridgeProtocolError(CODE_AUTH_FAILED, "missing bearer token")
        rec = self._table(kind).get(_hash(token))
        if rec is None or rec.revoked:
            raise BridgeProtocolError(CODE_AUTH_FAILED, "invalid token")
        return rec.user_id

    # ------------------------------------------------------------- revocation
    def revoke_user(self, kind: str, user_id: str) -> int:
        """Revoke every token of `kind` belonging to `user_id`. Returns how
        many were revoked. The other kind is untouched (independent
        revocation per direction)."""
        n = 0
        for rec in self._table(kind).values():
            if rec.user_id == user_id and not rec.revoked:
                rec.revoked = True
                n += 1
        return n

    def revoke_token(self, kind: str, token: str) -> bool:
        """Revoke one specific token. Returns True if it was known and live."""
        rec = self._table(kind).get(_hash(token))
        if rec is None or rec.revoked:
            return False
        rec.revoked = True
        return True

    # ------------------------------------------------------------------ ops
    def list_user(self, kind: str, user_id: str) -> list[dict]:
        """Metadata for a user's tokens (no plaintext, no full hashes)."""
        return [
            {
                "kind": kind,
                "user_id": rec.user_id,
                "hint": rec.hint,
                "created_at": rec.created_at,
                "revoked": rec.revoked,
            }
            for rec in self._table(kind).values()
            if rec.user_id == user_id
        ]
