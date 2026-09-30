"""Split bearer tokens: one namespace for the MCP side, one for the bridge.

任务书 v1.1 §七评审补遗: the single shared token is replaced by two
independent token kinds:

  * **mcp_token**    — presented by Octop's connector as `Authorization:
                       Bearer` on the /mcp/localoctop/ endpoint.
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
import threading
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
    # v0.6.0 热加载: True for records derived from the token env vars
    # (seeded at startup or reconciled by a reload). These follow the env
    # strictly — the user disappearing from the env removes the token.
    # Runtime-issued tokens (from the admin issuing endpoint) are False and
    # survive env reloads as long as their user is not governed by the env.
    from_env: bool = False


@dataclass
class TokenStore:
    """Holds both token namespaces, keyed by SHA-256 hash of the plaintext.

    v0.6.0 hot-reload: all mutating methods replace the per-kind table with a
    fresh dict under `_lock` (write-lock, read-copy), and `authenticate` grabs
    the lock only to take a reference — verification itself runs on the
    immutable snapshot outside the lock. Readers therefore never iterate a
    dict that is being mutated ("禁止边遍历边改"), and a concurrent reload
    swaps the whole table atomically.
    """

    _mcp: dict[str, TokenRecord] = field(default_factory=dict)
    _bridge: dict[str, TokenRecord] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _table(self, kind: str) -> dict[str, TokenRecord]:
        if kind == KIND_MCP:
            return self._mcp
        if kind == KIND_BRIDGE:
            return self._bridge
        raise ValueError(f"unknown token kind: {kind}")

    def _table_locked(self, kind: str) -> dict[str, TokenRecord]:
        """Snapshot of the current table under the lock; the caller mutates
        the copy freely (it is private after the swap in apply_env)."""
        with self._lock:
            return dict(self._table(kind))

    # -------------------------------------------------------------- issuing
    def issue(self, kind: str, user_id: str) -> str:
        """Mint a fresh token. Returns the plaintext — the only time it is
        ever available; only its hash is kept."""
        token = _PREFIX[kind] + secrets.token_hex(24)
        rec = TokenRecord(
            user_id=user_id,
            created_at=time.time(),
            hint=token[: len(_PREFIX[kind]) + 8],
        )
        with self._lock:
            old = dict(self._table(kind))
            old[_hash(token)] = rec
            self._set_table(kind, old)
        return token

    def seed(self, kind: str, user_id: str, plaintext: str) -> None:
        """Register a pre-existing plaintext (dev/test bootstrap from env
        vars; marks the record env-sourced so reloads can govern it).
        Production deployments issue via the admin endpoint instead."""
        if not plaintext:
            return
        rec = TokenRecord(
            user_id=user_id,
            created_at=time.time(),
            hint=plaintext[:16],
            from_env=True,
        )
        with self._lock:
            old = dict(self._table(kind))
            old[_hash(plaintext)] = rec
            self._set_table(kind, old)

    def _set_table(self, kind: str, table: dict[str, TokenRecord]) -> None:
        """Replace the whole table for `kind` with `table` (caller holds
        `_lock`). Authentication never sees a half-updated store: it reads
        either the old or the new table in full."""
        if kind == KIND_MCP:
            self._mcp = table
        elif kind == KIND_BRIDGE:
            self._bridge = table
        else:
            raise ValueError(f"unknown token kind: {kind}")

    # ------------------------------------------------------------ env reload
    def apply_env(self, kind: str, entries: dict[str, str]) -> dict[str, list[str]]:
        """Reconcile env-sourced tokens of `kind` against the current table.

        v0.6.0 热加载 semantics:
          * env 用户/令牌新增 → 插入
          * env 中消失的用户/令牌 → 移除（旧令牌随即使鉴权失败）
          * 运行时经 /admin/tokens/issue 签发、且用户不在 env 中的令牌
            **保留**（动态签发不受 env 轮询影响）；用户在 env 中的动态
            签发令牌按移除处理（运维以 env 为该用户的最终权威）。
        `entries` maps user_id -> plaintext as parsed from the env var.
        Returns {"added": [user_ids], "removed": [user_ids]} (no plaintext).
        The swap is atomic per kind.
        """
        env_hash_user: dict[str, str] = {}   # hash -> user_id (from env)
        env_users: set[str] = set()
        for user_id, plaintext in entries.items():
            h = _hash(plaintext)
            env_hash_user[h] = user_id
            env_users.add(user_id)

        old = self._table_locked(kind)
        new: dict[str, TokenRecord] = {}
        added: set[str] = set()
        removed: set[str] = set()

        # keep env tokens (re-seeded so a changed plaintext refreshes the record)
        for h, user_id in env_hash_user.items():
            rec = old.get(h)
            if rec is None:
                added.add(user_id)
            new[h] = TokenRecord(
                user_id=user_id,
                created_at=rec.created_at if rec is not None else time.time(),
                revoked=rec.revoked if rec is not None else False,
                hint=rec.hint if rec is not None else "",
                from_env=True,
            )
        # drop stale entries: a user whose env token is gone (rotated away or
        # removed from the env var) loses every record that is not the current
        # env hash — 移除即拒绝. Users absent from env keep their runtime-issued
        # tokens (dynamic issuance is untouched by env polling).
        env_plaintext_hashes = set(env_hash_user)
        for h, rec in old.items():
            if h in env_plaintext_hashes:
                continue
            if rec.user_id in env_users or rec.from_env:
                removed.add(rec.user_id)
                continue
            new[h] = rec

        with self._lock:
            self._set_table(kind, new)
        return {"added": sorted(added), "removed": sorted(removed)}

    # ---------------------------------------------------------- verification
    def authenticate(self, kind: str, token: str | None) -> str:
        """Return the user_id behind a valid token of `kind`, or raise
        CODE_AUTH_FAILED. Tokens of the *other* kind are just unknown here —
        this is what makes the two directions mutually exclusive."""
        if not token:
            raise BridgeProtocolError(CODE_AUTH_FAILED, "missing bearer token")
        with self._lock:
            table = self._table(kind)
        rec = table.get(_hash(token))
        if rec is None or rec.revoked:
            raise BridgeProtocolError(CODE_AUTH_FAILED, "invalid token")
        return rec.user_id

    # ------------------------------------------------------------- revocation
    def revoke_user(self, kind: str, user_id: str) -> int:
        """Revoke every token of `kind` belonging to `user_id`. Returns how
        many were revoked. The other kind is untouched (independent
        revocation per direction)."""
        n = 0
        old = self._table_locked(kind)
        new = {h: rec for h, rec in old.items()}
        for h, rec in new.items():
            if rec.user_id == user_id and not rec.revoked:
                # TokenRecord is shared with the immutable snapshot readers may
                # still hold — replace instead of mutating in place.
                new[h] = TokenRecord(
                    user_id=rec.user_id, created_at=rec.created_at,
                    revoked=True, hint=rec.hint,
                )
                n += 1
        if n:
            with self._lock:
                self._set_table(kind, new)
        return n

    def revoke_token(self, kind: str, token: str) -> bool:
        """Revoke one specific token. Returns True if it was known and live."""
        h = _hash(token)
        old = self._table_locked(kind)
        rec = old.get(h)
        if rec is None or rec.revoked:
            return False
        new = dict(old)
        new[h] = TokenRecord(
            user_id=rec.user_id, created_at=rec.created_at,
            revoked=True, hint=rec.hint,
        )
        with self._lock:
            self._set_table(kind, new)
        return True

    # ------------------------------------------------------------------ ops
    def list_user(self, kind: str, user_id: str) -> list[dict]:
        """Metadata for a user's tokens (no plaintext, no full hashes)."""
        table = self._table_locked(kind)
        return [
            {
                "kind": kind,
                "user_id": rec.user_id,
                "hint": rec.hint,
                "created_at": rec.created_at,
                "revoked": rec.revoked,
            }
            for rec in table.values()
            if not user_id or rec.user_id == user_id
        ]
