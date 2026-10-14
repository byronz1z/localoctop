"""Tests for the out-of-band transfer manager (LOX-002, T1):
handle-anchored confinement. The user's own folder is hostile ground —
every name in it can be swapped or turned into a symlink at any moment — so
these tests attack the adapter mid-transfer and assert that nothing outside
the user's folder is ever read, written, renamed or deleted. The transfer
ledger is the service's own book: it lives in LOCALOCTOP_STATE_DIR (a
pytest tmp dir here), never in the user's folder.

The bridge side is faked: a stub registry answers transfer_upload /
transfer_download like the Go bridge would, and chunks are pushed through
the real FastAPI routes via httpx. All file operations run for real in the
Linux test container; nothing about the filesystem is mocked.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import sys
import threading
import time
import urllib.parse

import httpx
import pytest

from localoctop.errors import (
    BridgeProtocolError,
    CODE_INVALID_PARAMS,
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_TOO_LARGE,
)
from localoctop.transfers import TransferManager
import localoctop.transfers as transfers_mod

from conftest import make_token_store

needs_symlink = pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation needs privileges the test shell does not grant on Windows")


class _Settings:
    bridge_timeout = 2.0

    def __init__(self, token_store):
        self.token_store = token_store


class FakeRegistry:
    """Answers bridge calls the way the Go bridge does: transfer_upload
    (stat_only) returns size/mtime; transfer_download accepts and reports a
    final path. Recorded calls let tests assert what was requested.

    Like the real bridge (transfer.go), a transfer_upload carrying
    max_bytes is refused with error 4003 when the file is over that limit —
    stat_only probes carry no max_bytes, so they always answer. A fake that
    ignored max_bytes would hide exactly the round-4 bug (the adapter
    asking for a stat WITH max_bytes and dying on the 4003)."""

    def __init__(self, files: dict[str, tuple[bytes, int]] | None = None):
        # files: local_path -> (content, mtime_unix)
        self.files = files or {}
        self.calls: list[tuple[str, dict]] = []

    async def call_paired(self, user_id, pair_id, method, params, timeout=None):
        return await self.call(user_id, method, params, timeout=timeout)

    async def call(self, user_id, method, params, timeout=None):
        self.calls.append((method, params))
        if method == "transfer_upload":
            content, mtime = self.files[params["path"]]
            max_bytes = int(params.get("max_bytes") or 0)
            if max_bytes > 0 and len(content) > max_bytes:
                raise BridgeProtocolError(
                    CODE_TOO_LARGE,
                    f"file is {len(content)} bytes, larger than the {max_bytes}-byte transfer limit")
            return {"accepted": True, "path": params["path"], "name": os.path.basename(params["path"]),
                    "size": len(content), "mtime_unix": mtime}
        if method == "transfer_download":
            return {"accepted": True, "path": params["path"], "name": os.path.basename(params["path"]),
                    "size": params.get("size", 0)}
        raise AssertionError(f"unexpected bridge call {method}")


@pytest.fixture
def users_dir(tmp_path, monkeypatch):
    d = tmp_path / "users"
    d.mkdir()
    monkeypatch.setenv("LOCALOCTOP_USERS_DIR", str(d))
    monkeypatch.setenv("LOCALOCTOP_LANDING_SUBDIR", "localoctop")
    monkeypatch.setenv("LOCALOCTOP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("LOCALOCTOP_MAX_TRANSFER_BYTES", raising=False)
    return d


@pytest.fixture
def mgr(users_dir):
    store = make_token_store()  # alice + bob strict pairs
    return TransferManager(_Settings(store), FakeRegistry(), "/mcp/localoctop/transfer")


def _mk_user(users_dir, name: str):
    p = users_dir / name
    p.mkdir()
    return p


async def _push_upload(mgr: TransferManager, client: httpx.AsyncClient,
                       user: str, local_path: str, content: bytes,
                       chunk_at: int | None = None, bad_sha: bool = False):
    """Start an upload and drive it to completion through the HTTP routes.
    With chunk_at, the content is sent as two chunks [0:chunk_at] +
    [chunk_at:]. Returns (transfer, last_http_response)."""
    t = await mgr.start_upload(user, "pair-1", local_path)
    assert t.state == "active"
    auth = {"Authorization": "Bearer btok-alice" if user == "alice" else "Bearer btok-bob",
            "X-Transfer-Ticket": t.ticket}
    base = f"/mcp/localoctop/transfer/{t.id}"
    pieces = [content] if chunk_at is None else [content[:chunk_at], content[chunk_at:]]
    offset = 0
    resp = None
    for piece in pieces:
        resp = await client.put(f"{base}/chunk?offset={offset}", content=piece, headers=auth)
        assert resp.status_code == 200, resp.text
        offset += len(piece)
    sha = hashlib.sha256(content).hexdigest()
    if bad_sha:
        sha = "0" * 64
    resp = await client.post(f"{base}/complete", json={"ok": True, "size": len(content), "sha256": sha},
                             headers=auth)
    assert resp.status_code == 200
    return t, resp


def _client_for(mgr: TransferManager) -> httpx.AsyncClient:
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(mgr.build_router())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _read_ledger(mgr: TransferManager, user: str) -> dict:
    p = mgr.ledger_dir / (urllib.parse.quote(user, safe="") + ".json")
    return json.loads(p.read_text("utf-8"))


def _ledger_path(mgr: TransferManager, user: str):
    return mgr.ledger_dir / (urllib.parse.quote(user, safe="") + ".json")


def _landing(users_dir, user: str):
    return users_dir / user / "localoctop"


# ------------------------------------------------------------ user names

class TestUserNameSafety:
    def test_chinese_name_accepted(self, users_dir, mgr):
        _mk_user(users_dir, "马威")
        fd = mgr._user_root_fd("马威")
        os.close(fd)

    def test_plain_names_accepted(self, users_dir, mgr):
        _mk_user(users_dir, "ann.a-smith_2")
        fd = mgr._user_root_fd("ann.a-smith_2")
        os.close(fd)

    @pytest.mark.parametrize("bad", ["", "   ", "\t", ".", "..", "a/b", "a\\b",
                                     "ma\x00li", "x\ny", "x\x7fy",
                                     # C1 control block must be refused too.
                                     "x\x85y", "x\x9fy",
                                     ])
    def test_unsafe_names_rejected(self, mgr, bad):
        with pytest.raises(BridgeProtocolError) as ei:
            mgr._user_root_fd(bad)
        assert ei.value.code == CODE_INVALID_PARAMS

    def test_dot_leading_name_accepted(self, users_dir, mgr):
        """Round 6: the upstream user-name rule (Octop patch 21) allows a
        leading dot; only '.', '..', separators, blanks and control
        characters are refused."""
        _mk_user(users_dir, ".hidden")
        fd = mgr._user_root_fd(".hidden")
        os.close(fd)

    async def test_dot_leading_user_can_transfer(self, users_dir, mgr):
        _mk_user(users_dir, ".hidden")
        mgr.registry = FakeRegistry({"f.txt": (b"hi", 1000)})
        t = await mgr.start_upload(".hidden", "pair-1", "f.txt")
        assert t.state == "active"
        assert t.dirs is not None and t.file_fd >= 0
        t.finish(True)  # release the held fds

    async def test_missing_user_folder_refused_and_not_created(self, users_dir, mgr):
        reg = FakeRegistry({"doc.txt": (b"hello", 1000)})
        mgr.registry = reg
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_upload("ghost", "pair-1", "doc.txt")
        assert ei.value.code == CODE_NOT_FOUND
        assert "no Octop user named 'ghost'" in ei.value.message
        # Nothing was created for the unknown account, and the bridge was
        # never asked anything.
        assert not (users_dir / "ghost").exists()
        assert reg.calls == []

    def test_user_folder_symlink_refused(self, users_dir, mgr):
        real = users_dir / "real"
        real.mkdir()
        try:
            (users_dir / "linked").symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("symlink not permitted")
        with pytest.raises(BridgeProtocolError) as ei:
            mgr._user_root_fd("linked")
        assert ei.value.code == CODE_NOT_ALLOWED


# ------------------------------------------------------------ upload path

class TestUploadLanding:
    async def test_landing_path_and_visible_cloud_path(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"Work/reports/q1.pdf": (b"data", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "Work/reports/q1.pdf", b"data")
        assert t.state == "done"
        dest = users_dir / "alice" / "localoctop" / "Work" / "reports" / "q1.pdf"
        assert dest.read_bytes() == b"data"
        # The agent-visible path is relative to the user's own folder.
        assert t.result["visible"] == "/localoctop/Work/reports/q1.pdf"
        assert "cloud_path: /localoctop/Work/reports/q1.pdf" in mgr.describe(t)

    async def test_absolute_local_path_keeps_only_name(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"D:/docs/secret.txt": (b"x", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "D:/docs/secret.txt", b"x")
        assert (users_dir / "alice" / "localoctop" / "secret.txt").read_bytes() == b"x"
        assert t.result["visible"] == "/localoctop/secret.txt"


# ------------------------------------------------------------ download confinement

class TestDownloadConfinement:
    async def test_read_inside_own_folder_ok(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        (home / "out.txt").write_bytes(b"payload")
        t = await mgr.start_download("alice", "pair-1", "/out.txt", "", False)
        assert t.size == 7
        assert t.expected_sha == hashlib.sha256(b"payload").hexdigest()
        t.finish(True)  # release the held fds

    async def test_traversal_rejected(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        (home / "f.txt").write_bytes(b"a")
        _mk_user(users_dir, "bob")
        (users_dir / "bob" / "secret.txt").write_bytes(b"bob-secret")
        for raw in ("/../bob/secret.txt", "/..%2f..%2fbob/secret.txt"):
            with pytest.raises(BridgeProtocolError) as ei:
                await mgr.start_download("alice", "pair-1", raw, "", False)
            assert ei.value.code in (CODE_NOT_ALLOWED, CODE_NOT_FOUND, CODE_INVALID_PARAMS)

    async def test_other_user_path_rejected(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "secret.txt").write_bytes(b"bob-secret")
        # Absolute host paths are not agent paths at all: the agent's root is
        # its own folder, so this can never name bob's file.
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_download("alice", "pair-1", str(bob / "secret.txt").replace("\\", "/"), "", False)
        assert ei.value.code in (CODE_NOT_ALLOWED, CODE_NOT_FOUND, CODE_INVALID_PARAMS)

    @needs_symlink
    async def test_symlink_to_other_user_rejected(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "secret.txt").write_bytes(b"bob-secret")
        (home / "peek.txt").symlink_to(bob / "secret.txt")
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_download("alice", "pair-1", "/peek.txt", "", False)
        assert ei.value.code == CODE_NOT_ALLOWED

    @needs_symlink
    async def test_symlink_dir_to_other_user_rejected(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "secret.txt").write_bytes(b"bob-secret")
        (home / "peekdir").symlink_to(bob, target_is_directory=True)
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_download("alice", "pair-1", "/peekdir/secret.txt", "", False)
        assert ei.value.code == CODE_NOT_ALLOWED

    async def test_missing_file_not_found(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_download("alice", "pair-1", "/nope.txt", "", False)
        assert ei.value.code == CODE_NOT_FOUND

    async def test_directory_is_not_a_file(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        (home / "dir").mkdir()
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_download("alice", "pair-1", "/dir", "", False)
        assert ei.value.code == CODE_NOT_FOUND

    async def test_relative_path_rejected(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_download("alice", "pair-1", "out.txt", "", False)
        assert ei.value.code == CODE_INVALID_PARAMS

    @needs_symlink
    async def test_source_swapped_for_symlink_mid_transfer(self, users_dir, mgr):
        """After the download started (source opened), the user replaces the
        source name with a symlink to another user's file. /content must
        still serve the ORIGINAL bytes (the adapter holds the fd) — never
        the other user's file."""
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        original = b"alice-original-payload"
        (home / "doc.txt").write_bytes(original)
        (bob / "secret.txt").write_bytes(b"bob-secret")
        t = await mgr.start_download("alice", "pair-1", "/doc.txt", "", False)
        # The swap happens after start, before the first /content read.
        os.unlink(home / "doc.txt")
        (home / "doc.txt").symlink_to(bob / "secret.txt")
        auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
        async with _client_for(mgr) as client:
            r = await client.get(f"/mcp/localoctop/transfer/{t.id}/content", headers=auth)
            assert r.status_code == 206
            assert r.content == original
            assert r.content != b"bob-secret"
            # A ranged resume after a fake disconnect serves the same file.
            r2 = await client.get(f"/mcp/localoctop/transfer/{t.id}/content",
                                  headers={**auth, "Range": "bytes=6-"})
            assert r2.status_code == 206
            assert r2.content == original[6:]
        t.finish(True)


# ------------------------------------------------------- write-side links

class TestWriteSideSymlinkDefence:
    @needs_symlink
    async def test_landing_subdir_symlink_escape_refused(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        # The agent plants a link where uploads would land.
        (users_dir / "alice" / "localoctop").symlink_to(bob, target_is_directory=True)
        mgr.registry = FakeRegistry({"evil.txt": (b"pwned", 1000)})
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_upload("alice", "pair-1", "evil.txt")
        assert ei.value.code == CODE_NOT_ALLOWED
        # Nothing landed in bob's folder.
        assert list(bob.iterdir()) == []

    @needs_symlink
    async def test_nested_symlink_escape_refused(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        landing = home / "localoctop"
        landing.mkdir()
        (landing / "docs").symlink_to(bob, target_is_directory=True)
        mgr.registry = FakeRegistry({"docs/evil.txt": (b"pwned", 1000)})
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_upload("alice", "pair-1", "docs/evil.txt")
        assert ei.value.code == CODE_NOT_ALLOWED
        assert list(bob.iterdir()) == []

    @needs_symlink
    async def test_existing_symlink_at_destination_saved_aside(self, users_dir, mgr):
        """Round 7: a symlink planted at the final name must neither be
        written through nor deleted — the upload lands under
        "<name>.uploaded", bob's file is untouched, and the result says so
        (replaces round 2's "refused" behaviour)."""
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "target.txt").write_bytes(b"bob-file")
        landing = home / "localoctop"
        landing.mkdir()
        (landing / "evil.txt").symlink_to(bob / "target.txt")
        mgr.registry = FakeRegistry({"evil.txt": (b"pwned", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "evil.txt", b"pwned")
        assert t.state == "done", t.error
        assert (landing / "evil.txt").is_symlink()
        assert os.readlink(landing / "evil.txt") == str(bob / "target.txt")
        assert (bob / "target.txt").read_bytes() == b"bob-file"
        assert (landing / "evil.txt.uploaded").read_bytes() == b"pwned"
        assert "cloud_path: /localoctop/evil.txt.uploaded" in mgr.describe(t)

    @needs_symlink
    async def test_landing_dir_swapped_mid_upload(self, users_dir, mgr):
        """The landing directory is replaced by a symlink to bob's folder
        between two chunks. The adapter writes through directory handles it
        opened before the swap, so the rest of the upload keeps landing in
        the REAL landing folder: bob gains nothing, loses nothing."""
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "keep.txt").write_bytes(b"bob-keep")
        bob_before = {p.name: p.read_bytes() for p in bob.iterdir()}
        content = b"0123456789" * 1000
        mgr.registry = FakeRegistry({"f.bin": (content, 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
        base = f"/mcp/localoctop/transfer/{t.id}"
        async with _client_for(mgr) as client:
            r = await client.put(f"{base}/chunk?offset=0", content=content[:4096], headers=auth)
            assert r.status_code == 200, r.text
            # The swap: the whole landing dir now points at bob's folder.
            real_landing = home / "localoctop"
            os.rename(real_landing, home / "localoctop-away")
            (home / "localoctop").symlink_to(bob, target_is_directory=True)
            r = await client.put(f"{base}/chunk?offset=4096", content=content[4096:], headers=auth)
            assert r.status_code == 200, r.text
            sha = hashlib.sha256(content).hexdigest()
            r = await client.post(f"{base}/complete",
                                  json={"ok": True, "size": len(content), "sha256": sha},
                                  headers=auth)
            assert r.status_code == 200
        assert t.state == "done", t.error
        # bob's folder is untouched: nothing added, nothing changed.
        assert {p.name: p.read_bytes() for p in bob.iterdir()} == bob_before
        # The file landed in the real landing folder (the one that was open).
        assert (home / "localoctop-away" / "f.bin").read_bytes() == content
        t.finish(True) if t.state == "active" else None

    @needs_symlink
    async def test_landing_dir_symlink_swap_on_failed_upload(self, users_dir, mgr):
        """Same swap, but the upload then fails integrity: the abort path
        must also stay inside alice's real folder."""
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "keep.txt").write_bytes(b"bob-keep")
        bob_before = {p.name: p.read_bytes() for p in bob.iterdir()}
        content = b"0123456789" * 1000
        mgr.registry = FakeRegistry({"f.bin": (content, 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
        base = f"/mcp/localoctop/transfer/{t.id}"
        async with _client_for(mgr) as client:
            r = await client.put(f"{base}/chunk?offset=0", content=content[:4096], headers=auth)
            assert r.status_code == 200, r.text
            real_landing = home / "localoctop"
            os.rename(real_landing, home / "localoctop-away")
            (home / "localoctop").symlink_to(bob, target_is_directory=True)
            r = await client.post(f"{base}/complete",
                                  json={"ok": True, "size": len(content), "sha256": "0" * 64},
                                  headers=auth)
            assert r.status_code == 200
        assert t.state == "failed"
        assert {p.name: p.read_bytes() for p in bob.iterdir()} == bob_before
        # No stray names in alice's real landing folder either — the
        # landing process never leaves a visible intermediate name.
        assert list((home / "localoctop-away").iterdir()) == []


# ------------------------------------------------------------ chunking

class TestChunkedUpload:
    async def test_offset_mismatch_409_with_received(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        content = b"0123456789"
        mgr.registry = FakeRegistry({"f.bin": (content, 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
        async with _client_for(mgr) as client:
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=3",
                                 content=b"xx", headers=auth)
            assert r.status_code == 409
            assert r.json()["received"] == 0
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                                 content=content, headers=auth)
            assert r.status_code == 200 and r.json()["received"] == len(content)
            # Re-sending at a stale offset reports what was durably received.
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                                 content=b"x", headers=auth)
            assert r.status_code == 409
            assert r.json()["received"] == len(content)
        t.finish(True) if t.state == "active" else None

    async def test_two_chunk_upload_completes_and_cleans_up(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        content = os.urandom(9000)
        mgr.registry = FakeRegistry({"big.bin": (content, 1234)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "big.bin", content, chunk_at=4096)
        assert t.state == "done"
        dest = users_dir / "alice" / "localoctop" / "big.bin"
        assert dest.read_bytes() == content
        assert int(dest.stat().st_mtime) == 1234
        # No leftover intermediate or partial names of any kind, and NO
        # service-owned file in the user's folder at all — the uploaded
        # file is the only name there.
        assert not list(dest.parent.glob("*.part-*"))
        assert [p.name for p in dest.parent.iterdir()] == ["big.bin"]
        idx = _read_ledger(mgr, "alice")
        ent = idx["pair-1|big.bin"]
        assert ent["size"] == len(content) and ent["sha256"] == hashlib.sha256(content).hexdigest()
        # The ledger records the landed copy's size and mtime (ns).
        st = dest.stat()
        assert ent["landed_size"] == st.st_size
        assert ent["landed_mtime_ns"] == st.st_mtime_ns
        assert t.result["sha256"] == ent["sha256"]
        # The ledger file itself is the service's own book: 0600.
        import stat as statmod
        assert statmod.S_IMODE(_ledger_path(mgr, "alice").stat().st_mode) == 0o600

    async def test_integrity_mismatch_fails_and_cleans_up(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        content = b"payload"
        mgr.registry = FakeRegistry({"f.bin": (content, 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.bin", content, bad_sha=True)
        assert t.state == "failed"
        assert "integrity check failed" in t.error
        landing = users_dir / "alice" / "localoctop"
        assert not (landing / "f.bin").exists()
        assert not list(landing.glob("*.part-*"))
        assert not [p for p in landing.iterdir() if p.name.startswith(".localoctop-")]

    async def test_forbidden_ticket_and_wrong_user(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.bin": (b"abc", 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        async with _client_for(mgr) as client:
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0", content=b"abc",
                                 headers={"Authorization": "Bearer btok-alice",
                                          "X-Transfer-Ticket": "wrong"})
            assert r.status_code == 403
            # bob's bridge token must not push chunks into alice's transfer.
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0", content=b"abc",
                                 headers={"Authorization": "Bearer btok-bob",
                                          "X-Transfer-Ticket": t.ticket})
            assert r.status_code == 403
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0", content=b"abc")
            assert r.status_code == 401
        t.finish(True) if t.state == "active" else None


# ------------------------------------------------------------ fresh check

class TestFreshCheck:
    async def test_same_version_not_resent(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        content = b"same-bytes"
        reg = FakeRegistry({"report.xlsx": (content, 4242)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "report.xlsx", content)
        assert t1.state == "done" and not t1.result.get("fresh")
        calls_after_first = len(reg.calls)
        # Second fetch of an unchanged file: stat call only, no data transfer.
        t2 = await mgr.start_upload("alice", "pair-1", "report.xlsx")
        assert t2.state == "done" and t2.result.get("fresh") is True
        assert t2.received == len(content)
        assert "nothing re-sent" in mgr.describe(t2)
        assert len(reg.calls) == calls_after_first + 1  # only the stat_only call
        assert reg.calls[-1][1].get("stat_only") is True

    async def test_changed_version_is_resent(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        reg = FakeRegistry({"report.xlsx": (b"v1", 1000)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "report.xlsx", b"v1")
            assert t1.state == "done"
            # Same name, new content and mtime -> a real second transfer.
            reg.files["report.xlsx"] = (b"v2-longer", 2000)
            t2, _ = await _push_upload(mgr, client, "alice", "report.xlsx", b"v2-longer")
        assert t2.state == "done" and not t2.result.get("fresh")
        assert (users_dir / "alice" / "localoctop" / "report.xlsx").read_bytes() == b"v2-longer"

    async def test_cloud_file_replaced_same_size_is_resent(self, users_dir, mgr):
        """The cloud copy is swapped for different bytes of the SAME size
        after the first upload (mtime_ns changes). 'Already up to date' must
        not trust the old index entry — the file is re-sent."""
        _mk_user(users_dir, "alice")
        content = b"0123456789"
        reg = FakeRegistry({"cfg.yaml": (content, 1000)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "cfg.yaml", content)
            assert t1.state == "done"
            dest = users_dir / "alice" / "localoctop" / "cfg.yaml"
            assert dest.read_bytes() == content
            # The user edits the cloud copy: same length, other bytes.
            dest.write_bytes(b"9876543210")
            calls_after = len(reg.calls)
            t2, _ = await _push_upload(mgr, client, "alice", "cfg.yaml", content)
        assert t2.state == "done" and not t2.result.get("fresh")
        assert dest.read_bytes() == content
        # A real transfer ran again (stat + the upload start).
        assert len(reg.calls) == calls_after + 2

    async def test_cloud_file_missing_is_resent(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        content = b"data"
        reg = FakeRegistry({"f.txt": (content, 1000)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "f.txt", content)
            assert t1.state == "done"
            os.unlink(users_dir / "alice" / "localoctop" / "f.txt")
            t2, _ = await _push_upload(mgr, client, "alice", "f.txt", content)
        assert t2.state == "done" and not t2.result.get("fresh")


# ------------------------------------------------------------ limits (LOX-002 round 3)

class TestTransferLimits:
    async def test_upload_over_limit_is_advisory_not_sent(self, users_dir, mgr, monkeypatch):
        """A file over the transfer limit is a normal (isError=false) answer
        that names the size and the limit; the bridge is only asked for a
        stat, never to send bytes, and nothing appears in the cloud."""
        monkeypatch.setenv("LOCALOCTOP_MAX_TRANSFER_BYTES", "100")
        # Rebuild the manager so the env var is picked up.
        mgr = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        _mk_user(users_dir, "alice")
        reg = FakeRegistry({"big.bin": (b"x" * 500, 1000)})
        mgr.registry = reg
        t = await mgr.start_upload("alice", "pair-1", "big.bin")
        assert t.state == "failed" and t.result.get("advisory") is True
        text = mgr.describe(t)
        assert "NOT TRANSFERRED" in text
        assert "500" in text and "100" in text  # file size and the limit
        assert "split or compress" in text
        # Only the stat_only probe reached the bridge; no upload start.
        assert len(reg.calls) == 1 and reg.calls[0][1].get("stat_only") is True
        landing = users_dir / "alice" / "localoctop"
        assert not landing.exists() or list(landing.iterdir()) == []

    async def test_upload_over_default_1024mib_limit(self, users_dir, monkeypatch):
        """The default limit is 1024 MiB (was 2 GiB)."""
        monkeypatch.delenv("LOCALOCTOP_MAX_TRANSFER_BYTES", raising=False)
        mgr = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        assert mgr.max_bytes == 1024 * 1024 * 1024

    async def test_cloud_disk_shortage_is_advisory(self, users_dir, mgr, monkeypatch):
        """Not enough free space in the cloud -> normal advisory, no bytes
        requested, nothing landed."""
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "DISK_RESERVE_BYTES", 1 << 62)  # absurd reserve forces the branch
        _mk_user(users_dir, "alice")
        reg = FakeRegistry({"f.bin": (b"data", 1000)})
        mgr.registry = reg
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        assert t.state == "failed" and t.result.get("advisory") is True
        text = mgr.describe(t)
        assert "NOT TRANSFERRED" in text and "not enough space" in text
        assert len(reg.calls) == 1  # stat only
        landing = users_dir / "alice" / "localoctop"
        assert not [p for p in landing.iterdir() if p.name == "f.bin"]

    async def test_download_over_limit_is_advisory(self, users_dir, mgr, monkeypatch):
        monkeypatch.setenv("LOCALOCTOP_MAX_TRANSFER_BYTES", "10")
        mgr = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        home = _mk_user(users_dir, "alice")
        (home / "big.dat").write_bytes(b"x" * 100)
        t = await mgr.start_download("alice", "pair-1", "/big.dat", "", False)
        assert t.state == "failed" and t.result.get("advisory") is True
        text = mgr.describe(t)
        assert "NOT TRANSFERRED" in text and "100" in text and "10" in text

    async def test_upload_over_limit_real_bridge_4003_still_advisory(self, users_dir, mgr, monkeypatch):
        """Round-4 regression: the REAL bridge refuses a stat request that
        carries max_bytes with error 4003 before it ever reports the size
        (that is what the live bridge + adapter produced). The probe must
        therefore go out WITHOUT max_bytes; the adapter judges the limit
        itself. FakeRegistry now mimics the real bridge, so this test would
        fail with isError-style behaviour if the probe carried max_bytes."""
        monkeypatch.setenv("LOCALOCTOP_MAX_TRANSFER_BYTES", "100")
        mgr = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        _mk_user(users_dir, "alice")
        reg = FakeRegistry({"big.bin": (b"x" * 500, 1000)})
        mgr.registry = reg
        t = await mgr.start_upload("alice", "pair-1", "big.bin")
        assert t.state == "failed" and t.result.get("advisory") is True
        text = mgr.describe(t)
        assert "NOT TRANSFERRED" in text
        assert "500" in text and "100" in text
        assert "split or compress" in text
        # The probe reached the bridge without max_bytes (a real bridge
        # answers it); NO real upload start ever left the adapter.
        assert len(reg.calls) == 1
        assert reg.calls[0][1].get("stat_only") is True
        assert "max_bytes" not in reg.calls[0][1]
        landing = users_dir / "alice" / "localoctop"
        assert not landing.exists() or list(landing.iterdir()) == []

    async def test_upload_start_4003_after_probe_becomes_advisory(self, users_dir, mgr, monkeypatch):
        """The file fit under the limit at probe time but the bridge refuses
        the real upload start with 4003 (the file grew in between, or the
        bridge's own limit is tighter). Same outcome as a probe-time
        refusal: an advisory, no error, nothing landed, no leftover temp
        names, and the partially-prepared transfer's handles are released."""
        monkeypatch.setenv("LOCALOCTOP_MAX_TRANSFER_BYTES", "100")
        mgr = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        _mk_user(users_dir, "alice")

        class GrowsAtStart(FakeRegistry):
            async def call(self, user_id, method, params, timeout=None):
                if method == "transfer_upload" and not params.get("stat_only"):
                    # The file grew past the limit after the probe: the
                    # bridge does what transfer.go does with max_bytes.
                    self.calls.append((method, params))
                    raise BridgeProtocolError(
                        CODE_TOO_LARGE,
                        "file is 150 bytes, larger than the 100-byte transfer limit")
                return await super().call(user_id, method, params, timeout=timeout)

        reg = GrowsAtStart({"f.bin": (b"x" * 50, 1000)})  # 50 < 100 at probe time
        mgr.registry = reg
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        assert t.state == "failed" and t.result.get("advisory") is True
        text = mgr.describe(t)
        assert "NOT TRANSFERRED" in text and "100" in text
        # Probe + refused start: exactly two bridge calls, no chunk traffic.
        assert len(reg.calls) == 2
        assert reg.calls[0][1].get("stat_only") is True
        assert reg.calls[1][1].get("stat_only") is not True
        # Nothing appeared in the cloud and no temp/materialized name leaks.
        landing = users_dir / "alice" / "localoctop"
        if landing.exists():
            assert not [p for p in landing.rglob("*") if p.name == "f.bin"]
            assert not [p for p in landing.rglob("*") if p.name.startswith(".localoctop-")]
        # The refused transfer's record was finished (handles released), and
        # the record we hand back is the advisory one.
        actives = [x for x in mgr._transfers.values() if x.state == "active"]
        assert actives == []

    async def test_fetch_tool_renders_advisory_as_not_error(self, users_dir, mgr, monkeypatch):
        """End-to-end at the tools layer: isError is False for an advisory."""
        import time as _time
        from localoctop.tools import ToolService
        monkeypatch.setenv("LOCALOCTOP_MAX_TRANSFER_BYTES", "100")
        store = make_token_store()

        class S:
            bridge_timeout = 2.0

            def __init__(self):
                self.token_store = store

        reg = FakeRegistry({"big.bin": (b"x" * 500, 1000)})
        tm = TransferManager(S(), reg, "/mcp/localoctop/transfer")
        _mk_user(users_dir, "alice")
        svc = ToolService(S(), None, None)
        svc.transfers = tm

        class _Audit:
            async def record(self, **kw):
                pass
        svc.audit = _Audit()
        res = await svc._transfer_tool("alice", "pair-1", "fetch_to_cloud", {"path": "big.bin"},
                                       "big.bin", _time.monotonic(), "")
        assert res["isError"] is False
        text = res["content"][0]["text"]
        assert "NOT TRANSFERRED" in text and "500" in text


# ------------------------------------------------- cloud-edited keep-aside

class TestCloudEditedKept:
    async def test_edited_cloud_copy_kept_aside(self, users_dir, mgr):
        """The agent edited the landed cloud copy; re-fetching must rename
        the edited file aside (never overwrite it) and land the new version
        under the real name."""
        _mk_user(users_dir, "alice")
        reg = FakeRegistry({"doc.txt": (b"v1", 1000)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1")
            assert t1.state == "done"
            dest = users_dir / "alice" / "localoctop" / "doc.txt"
            # The cloud AI edits the copy.
            dest.write_bytes(b"cloud-edited-content")
            # The local file also moved on -> a real second transfer.
            reg.files["doc.txt"] = (b"v2", 2000)
            t2, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v2")
        assert t2.state == "done"
        # The edited copy survives under a .cloud-edited- name with its content.
        kept = [p for p in dest.parent.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1
        assert kept[0].name.startswith("doc.cloud-edited-") and kept[0].name.endswith(".txt")
        assert kept[0].read_bytes() == b"cloud-edited-content"
        # The real name now holds the new version.
        assert dest.read_bytes() == b"v2"
        # The success text mentions the kept-as name (agent view).
        assert "kept as" in mgr.describe(t2)
        assert ".cloud-edited-" in mgr.describe(t2)

    async def test_untouched_cloud_copy_replaced_without_keep(self, users_dir, mgr):
        """The cloud copy is exactly what we landed last time and the local
        file changed: the occupant is moved aside atomically, verified by
        content against the index, and removed — no .cloud-edited- file
        remains and nothing else is touched."""
        _mk_user(users_dir, "alice")
        reg = FakeRegistry({"doc.txt": (b"v1", 1000)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1")
            assert t1.state == "done"
            reg.files["doc.txt"] = (b"v2", 2000)
            t2, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v2")
        assert t2.state == "done"
        landing = users_dir / "alice" / "localoctop"
        assert not [p for p in landing.iterdir() if ".cloud-edited-" in p.name]
        assert (landing / "doc.txt").read_bytes() == b"v2"
        assert "kept as" not in mgr.describe(t2)

    async def test_unknown_cloud_file_kept_aside(self, users_dir, mgr):
        """A file at the landing name that the index has no record of (the
        agent created it) is somebody's work too: kept aside, not overwritten."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / "doc.txt").write_bytes(b"agent-made")
        reg = FakeRegistry({"doc.txt": (b"fresh", 1000)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"fresh")
        assert t.state == "done"
        kept = [p for p in landing.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1 and kept[0].read_bytes() == b"agent-made"
        assert (landing / "doc.txt").read_bytes() == b"fresh"


# ------------------------------------------------------------ permissions

class TestLandedFilePermissions:
    async def test_landed_file_is_0644_ledger_is_0600(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.txt": (b"hello", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.txt", b"hello")
        assert t.state == "done"
        import stat as statmod
        dest = users_dir / "alice" / "localoctop" / "f.txt"
        assert statmod.S_IMODE(dest.stat().st_mode) == 0o644
        assert statmod.S_IMODE(_ledger_path(mgr, "alice").stat().st_mode) == 0o600


# ------------------------------------------------------------ housekeeping
class TestHousekeeping:
    async def test_transfer_isolation_between_users(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        _mk_user(users_dir, "bob")
        mgr.registry = FakeRegistry({"f.txt": (b"x", 1)})
        # alice's transfer record must be invisible to bob.
        t = await mgr.start_upload("alice", "pair-1", "f.txt")
        with pytest.raises(BridgeProtocolError) as ei:
            mgr.get_for_user("bob", t.id)
        assert ei.value.code == CODE_NOT_FOUND
        assert mgr.get_for_user("alice", t.id) is t
        t.finish(True) if t.state == "active" else None

    async def test_legacy_link_name_survives_construction_and_upload(self, users_dir, mgr):
        """The adapter no longer sweeps by name (round 5): a file whose name
        starts with the old materialization prefix is somebody's data, never
        to be touched. It must survive manager construction AND a full
        upload with its content intact."""
        home = _mk_user(users_dir, "alice")
        landing = home / "localoctop"
        landing.mkdir()
        legacy = landing / ".localoctop-link-dead1"
        legacy.write_bytes(b"not-ours-to-delete")
        mgr2 = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        mgr2.registry = FakeRegistry({"f.bin": (b"payload", 1000)})
        async with _client_for(mgr2) as client:
            t, _ = await _push_upload(mgr2, client, "alice", "f.bin", b"payload")
        assert t.state == "done"
        assert legacy.read_bytes() == b"not-ours-to-delete"
        assert (landing / "f.bin").read_bytes() == b"payload"


# --------------------------------------------------- handles: no leaks (R5)

class TestHandleLeaks:
    def _fd_count(self) -> int:
        return len(os.listdir("/proc/self/fd"))

    async def test_upload_success_and_refusals_do_not_leak_fds(self, users_dir, mgr):
        """50 successful uploads + 50 refused requests (missing user folder,
        symlinked landing): the process fd count must not grow."""
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        reg = FakeRegistry({f"f{i}.bin": (os.urandom(600), 1000) for i in range(51)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            # Warm up once (first import / lazy setup costs fds legitimately).
            t, _ = await _push_upload(mgr, client, "alice", "f0.bin", reg.files["f0.bin"][0])
            assert t.state == "done"
            try:
                await mgr.start_upload("ghost", "pair-1", "f0.bin")
            except BridgeProtocolError:
                pass
            before = self._fd_count()
            for i in range(1, 51):
                t, _ = await _push_upload(mgr, client, "alice", f"f{i}.bin", reg.files[f"f{i}.bin"][0])
                assert t.state == "done"
            for _ in range(25):
                with pytest.raises(BridgeProtocolError):
                    await mgr.start_upload("ghost", "pair-1", "f0.bin")
            # A symlinked landing is refused too.
            os.rename(home / "localoctop", home / "localoctop-real")
            (home / "localoctop").symlink_to(bob, target_is_directory=True)
            for _ in range(25):
                with pytest.raises(BridgeProtocolError) as ei:
                    await mgr.start_upload("alice", "pair-1", "f0.bin")
                assert ei.value.code == CODE_NOT_ALLOWED
            assert self._fd_count() <= before

    async def test_download_success_and_refusals_do_not_leak_fds(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        (home / "out.txt").write_bytes(b"payload")
        async with _client_for(mgr) as client:
            # Warm up.
            t = await mgr.start_download("alice", "pair-1", "/out.txt", "", False)
            t.finish(True)
            with pytest.raises(BridgeProtocolError):
                await mgr.start_download("alice", "pair-1", "/nope.txt", "", False)
            before = self._fd_count()
            auth = {"Authorization": "Bearer btok-alice"}
            for _ in range(50):
                t = await mgr.start_download("alice", "pair-1", "/out.txt", "", False)
                assert t.state == "active"
                r = await client.get(f"/mcp/localoctop/transfer/{t.id}/content",
                                     headers={**auth, "X-Transfer-Ticket": t.ticket})
                assert r.status_code == 206 and r.content == b"payload"
                await client.post(f"/mcp/localoctop/transfer/{t.id}/complete",
                                  json={"ok": True, "path": "/local/out.txt"},
                                  headers={**auth, "X-Transfer-Ticket": t.ticket})
                assert t.state == "done"
            for _ in range(25):
                with pytest.raises(BridgeProtocolError):
                    await mgr.start_download("alice", "pair-1", "/nope.txt", "", False)
            for _ in range(25):
                with pytest.raises(BridgeProtocolError):
                    await mgr.start_download("ghost", "pair-1", "/out.txt", "", False)
            assert self._fd_count() <= before


# ------------------------------------------- background stall sweep (R5)

class TestBackgroundSweep:
    async def test_stalled_upload_aborted_and_handles_released(self, users_dir, mgr, monkeypatch):
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "STALL_SECONDS", 0.05)
        _mk_user(users_dir, "alice")
        content = b"0123456789" * 10
        mgr.registry = FakeRegistry({"f.bin": (content, 1000)})
        before = len(os.listdir("/proc/self/fd"))
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        assert t.state == "active"
        assert t.file_fd >= 0 and t.dirs is not None
        # Nobody ever asks for status; make it stale and sweep once directly.
        t.updated = time.monotonic() - 10
        await mgr.sweep_transfers()
        assert t.state == "failed" and "stopped making progress" in t.error
        assert t.file_fd == -1 and t.dirs is None
        assert len(os.listdir("/proc/self/fd")) <= before

    async def test_stalled_download_aborted_and_handles_released(self, users_dir, mgr, monkeypatch):
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "STALL_SECONDS", 0.05)
        home = _mk_user(users_dir, "alice")
        (home / "out.txt").write_bytes(b"payload")
        before = len(os.listdir("/proc/self/fd"))
        t = await mgr.start_download("alice", "pair-1", "/out.txt", "", False)
        assert t.state == "active"
        t.updated = time.monotonic() - 10
        await mgr.sweep_transfers()
        assert t.state == "failed"
        assert t.file_fd == -1 and t.dirs is None
        assert len(os.listdir("/proc/self/fd")) <= before

    async def test_finished_records_expired_by_sweep(self, users_dir, mgr, monkeypatch):
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "RECORD_TTL_SECONDS", 0.01)
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.bin": (b"x", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"x")
        assert t.id in mgr._transfers
        t.finished = time.monotonic() - 10
        await mgr.sweep_transfers()
        assert t.id not in mgr._transfers

    async def test_scan_task_started_lazily_and_stopped(self, users_dir, mgr):
        """The periodic task exists after the first transfer and is cancelled
        cleanly by aclose(); a second manager on the same loop works too."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.bin": (b"x", 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.bin")
        assert mgr._scan_task is not None and not mgr._scan_task.done()
        t.finish(True)
        await mgr.aclose()
        assert mgr._scan_task is None


# ----------------------------------- landing never overwrites (R5, item 3/4)

class TestLandingNeverOverwrites:
    async def test_unknown_occupant_moved_aside_content_intact(self, users_dir, mgr):
        """A file at the landing name the index has no record of: moved to a
        .cloud-edited- name unchanged, the new file takes the real name."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / "doc.txt").write_bytes(b"agent-made")
        mgr.registry = FakeRegistry({"doc.txt": (b"fresh", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"fresh")
        assert t.state == "done"
        kept = [p for p in landing.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1 and kept[0].read_bytes() == b"agent-made"
        assert (landing / "doc.txt").read_bytes() == b"fresh"

    async def test_same_size_same_mtime_edit_still_kept(self, users_dir, mgr):
        """The agent edits the landed copy keeping the SAME size and
        restoring the mtime (ns-exact): size+mtime say 'unchanged', the
        sha256 says otherwise. The edited copy must be kept, not deleted."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"doc.txt": (b"v1-aaaa", 1000)})
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1-aaaa")
            assert t1.state == "done"
            dest = users_dir / "alice" / "localoctop" / "doc.txt"
            st = dest.stat()
            # Same-size edit + exact mtime restore.
            dest.write_bytes(b"v1-BBBB")
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
            reg2 = FakeRegistry({"doc.txt": (b"v2-longer", 2000)})
            mgr.registry = reg2
            t2, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v2-longer")
        assert t2.state == "done"
        landing = dest.parent
        kept = [p for p in landing.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1 and kept[0].read_bytes() == b"v1-BBBB"
        assert dest.read_bytes() == b"v2-longer"

    async def test_untouched_previous_copy_removed_after_move(self, users_dir, mgr):
        """The occupant IS our last landing, untouched: it is moved aside
        atomically (never overwritten), verified by content, and removed —
        no .cloud-edited- file remains."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"doc.txt": (b"v1", 1000)})
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1")
            assert t1.state == "done"
            mgr.registry = FakeRegistry({"doc.txt": (b"v2", 2000)})
            t2, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v2")
        assert t2.state == "done"
        landing = users_dir / "alice" / "localoctop"
        assert not [p for p in landing.iterdir() if ".cloud-edited-" in p.name]
        assert (landing / "doc.txt").read_bytes() == b"v2"

    async def test_keep_name_collision_uses_next_suffix(self, users_dir, mgr):
        """The first .cloud-edited- candidate is taken: the next suffix is
        used and the pre-existing file's content is unchanged."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / "doc.txt").write_bytes(b"agent-made")
        mgr.registry = FakeRegistry({"doc.txt": (b"fresh", 1000)})

        real_keep = mgr._keep_cloud_edited

        def colliding(dirs, name):
            # Reproduce the real naming scheme, then occupy the first
            # candidate so the loop must take "-1".
            stem, dot, ext = name.rpartition(".")
            if dot:
                ext = "." + ext
            else:
                stem, ext = name, ""
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            holder = landing / f"{stem}.cloud-edited-{stamp}{ext}"
            if not holder.exists():
                holder.write_bytes(b"occupied-first")
            return real_keep(dirs, name)

        mgr._keep_cloud_edited = colliding
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"fresh")
        assert t.state == "done"
        kept = sorted(p for p in landing.iterdir() if ".cloud-edited-" in p.name)
        assert len(kept) == 2
        contents = sorted(p.read_bytes() for p in kept)
        assert contents == [b"agent-made", b"occupied-first"]
        assert (landing / "doc.txt").read_bytes() == b"fresh"

    async def test_no_intermediate_names_left_after_landing(self, users_dir, mgr):
        """After the whole landing dance (including a keep-aside), the
        landing folder holds only final names — no .localoctop-link-*, no
        temp names of any kind."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / "doc.txt").write_bytes(b"agent-made")
        mgr.registry = FakeRegistry({"doc.txt": (b"fresh", 1000), "other.bin": (b"yy", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"fresh")
            assert t.state == "done"
            t2, _ = await _push_upload(mgr, client, "alice", "other.bin", b"yy")
            assert t2.state == "done"
        for p in landing.iterdir():
            assert not p.name.startswith(".localoctop-link-")
        names = {p.name for p in landing.iterdir()}
        assert "doc.txt" in names and "other.bin" in names

    def test_renameat2_primary_path_works(self, users_dir):
        """The container's libc/kernel must take the real renameat2 path
        (not the link+unlink fallback): rename an occupied name with
        RENAME_NOREPLACE fails with EEXIST and leaves the occupant
        untouched, and a free name is moved atomically."""
        import localoctop.transfers as tr
        d = _mk_user(users_dir, "alice")
        (d / "src.txt").write_bytes(b"mine")
        (d / "dst.txt").write_bytes(b"theirs")
        fd = os.open(d, tr._O_DIR_FLAGS)
        try:
            with pytest.raises(FileExistsError):
                tr._rename_noreplace(fd, "src.txt", "dst.txt")
            assert (d / "dst.txt").read_bytes() == b"theirs"
            assert (d / "src.txt").read_bytes() == b"mine"
            tr._rename_noreplace(fd, "src.txt", "moved.txt")
            assert (d / "moved.txt").read_bytes() == b"mine"
            assert not (d / "src.txt").exists()
        finally:
            os.close(fd)
        assert tr._libc is not None, "glibc renameat2 unavailable in the test container"
        assert tr._renameat2_fallbacks == 0, "the test container took the link+unlink fallback"


# --------------------------------------- fresh check trusts content (R5, 4)

class TestFreshCheckContentVerified:
    async def test_same_size_restored_mtime_edit_is_resent(self, users_dir, mgr):
        """The cloud copy is edited keeping size AND restoring the mtime to
        the nanosecond: 'already up to date' must NOT fire — the file is
        re-sent and the edited copy kept aside."""
        _mk_user(users_dir, "alice")
        content = b"0123456789"
        reg = FakeRegistry({"cfg.yaml": (content, 4242)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "cfg.yaml", content)
            assert t1.state == "done"
            dest = users_dir / "alice" / "localoctop" / "cfg.yaml"
            st = dest.stat()
            dest.write_bytes(b"9876543210")  # same size, other bytes
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
            t2, _ = await _push_upload(mgr, client, "alice", "cfg.yaml", content)
        assert t2.state == "done" and not t2.result.get("fresh")
        assert dest.read_bytes() == content
        kept = [p for p in dest.parent.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1 and kept[0].read_bytes() == b"9876543210"

    async def test_truly_unchanged_copy_still_skips_resend(self, users_dir, mgr):
        """The happy path must survive the stricter check: a copy whose
        content still matches the index is NOT re-sent."""
        _mk_user(users_dir, "alice")
        content = b"same-bytes"
        reg = FakeRegistry({"report.xlsx": (content, 4242)})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "report.xlsx", content)
        assert t1.state == "done"
        calls_after_first = len(reg.calls)
        t2 = await mgr.start_upload("alice", "pair-1", "report.xlsx")
        assert t2.state == "done" and t2.result.get("fresh") is True
        assert len(reg.calls) == calls_after_first + 1  # only the stat probe


# --------------------------------------------------- LOX-002 round 6 tests

import asyncio

needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="os.mkfifo is unavailable on this platform")


class TestFifoNeverBlocks:
    """A named pipe the user's AI created must never hang the adapter: the
    open happens on the event loop thread, so a blocking open() would stall
    transfers for EVERY user (round 6, item 1)."""

    @needs_fifo
    async def test_fifo_as_download_source_fails_fast(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        os.mkfifo(home / "pipe")

        async def go():
            with pytest.raises(BridgeProtocolError) as ei:
                await mgr.start_download("alice", "pair-1", "/pipe", "", False)
            return ei.value
        exc = await asyncio.wait_for(go(), timeout=2.0)
        assert exc.code == CODE_NOT_FOUND
        assert "not a regular file" in exc.message

    @needs_fifo
    async def test_fifo_at_landing_name_saved_aside_complete_fast(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        landing = home / "localoctop"
        landing.mkdir()
        os.mkfifo(landing / "f.bin")
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})

        async def go():
            async with _client_for(mgr) as client:
                return await _push_upload(mgr, client, "alice", "f.bin", b"data")
        t, _ = await asyncio.wait_for(go(), timeout=2.0)
        assert t.state == "done", t.error
        import stat as statmod
        assert (landing / "f.bin.uploaded").read_bytes() == b"data"
        assert statmod.S_ISFIFO((landing / "f.bin").stat().st_mode)
        desc = mgr.describe(t)
        assert "f.bin.uploaded" in desc and "named pipe" in desc
        assert "cloud_path: /localoctop/f.bin.uploaded" in desc


class TestLedgerBadData:
    """The ledger file is machine-maintained, but anything on disk can be
    damaged: any garbage in it must read as 'no records', never as an
    exception (T1: the read path never raises into request handling)."""

    @pytest.mark.parametrize("payload", [
        b"", b"[]", b"null", b'{"x": 1}', b"\xff\xfe not utf-8",
    ])
    async def test_bad_ledger_content_upload_still_works(self, users_dir, mgr, payload, caplog):
        _mk_user(users_dir, "alice")
        mgr.ledger_dir.mkdir(parents=True, exist_ok=True)
        _ledger_path(mgr, "alice").write_bytes(payload)
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})
        with caplog.at_level("WARNING", logger="localoctop.transfers"):
            async with _client_for(mgr) as client:
                t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"data")
        assert t.state == "done", t.error
        landing = _landing(users_dir, "alice")
        assert (landing / "f.bin").read_bytes() == b"data"
        # '{"x": 1}' parses as an object with one bad entry — skipped
        # silently. Everything else here is whole-file damage: a warning.
        if payload and payload != b'{"x": 1}':
            assert any("ledger" in r.message for r in caplog.records)

    async def test_oversized_ledger_read_as_empty(self, users_dir, mgr, caplog):
        _mk_user(users_dir, "alice")
        mgr.ledger_dir.mkdir(parents=True, exist_ok=True)
        big = b'{"' + b"x" * (17 * 1024 * 1024) + b'": {}}'
        _ledger_path(mgr, "alice").write_bytes(big)
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})
        with caplog.at_level("WARNING", logger="localoctop.transfers"):
            async with _client_for(mgr) as client:
                t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"data")
        assert t.state == "done", t.error
        assert any("oversized" in r.message for r in caplog.records)

    async def test_deeply_nested_ledger_read_as_empty(self, users_dir, mgr, caplog):
        _mk_user(users_dir, "alice")
        mgr.ledger_dir.mkdir(parents=True, exist_ok=True)
        _ledger_path(mgr, "alice").write_bytes(b"[" * 100_000)
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})
        with caplog.at_level("WARNING", logger="localoctop.transfers"):
            async with _client_for(mgr) as client:
                t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"data")
        assert t.state == "done", t.error
        assert any("ledger" in r.message for r in caplog.records)

    async def test_bad_entry_fields_upload_still_works(self, users_dir, mgr, caplog):
        """Entries with wrong-typed fields (landed_name an int, size a
        string, sha256 a number) must never raise: the upload runs normally
        and bad entries are ignored."""
        _mk_user(users_dir, "alice")
        mgr.ledger_dir.mkdir(parents=True, exist_ok=True)
        _ledger_path(mgr, "alice").write_text(json.dumps({
            "_localoctop_ledger": 1,
            "pair-1|f.bin": {"size": "4", "mtime": 1000, "sha256": 42,
                             "landed_name": 7, "landed_size": 4, "landed_mtime_ns": 0},
        }), "utf-8")
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})
        with caplog.at_level("WARNING", logger="localoctop.transfers"):
            async with _client_for(mgr) as client:
                t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"data")
        assert t.state == "done", t.error
        landing = _landing(users_dir, "alice")
        assert (landing / "f.bin").read_bytes() == b"data"


class TestLedgerFilePerUser:
    """One ledger file per user, named by a reversible, safe-characters-only
    encoding of the user name — Chinese names, spaces, '%' and leading dots
    all get their own file and never collide."""

    @pytest.mark.parametrize("name", ["马威", "ann a", "per%cent", ".hidden"])
    async def test_special_names_get_own_safe_ledger(self, users_dir, tmp_path, monkeypatch, name):
        monkeypatch.setenv("LOCALOCTOP_STATE_DIR", str(tmp_path / "state"))
        store = make_token_store()
        store.seed_pair(name, f"tok-{len(name)}", f"btok-{len(name)}")
        m = TransferManager(_Settings(store), FakeRegistry({name + "-f.txt": (b"x", 1000)}),
                            "/mcp/localoctop/transfer")
        _mk_user(users_dir, name)
        async with _client_for(m) as client:
            t = await m.start_upload(name, "pair-1", name + "-f.txt")
            auth = {"Authorization": f"Bearer btok-{len(name)}", "X-Transfer-Ticket": t.ticket}
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                                 content=b"x", headers=auth)
            assert r.status_code == 200, r.text
            r = await client.post(f"/mcp/localoctop/transfer/{t.id}/complete",
                                  json={"ok": True, "size": 1,
                                        "sha256": hashlib.sha256(b"x").hexdigest()},
                                  headers=auth)
            assert r.status_code == 200
        assert t.state == "done", t.error
        p = _ledger_path(m, name)
        assert p.exists()
        # The file name carries only safe characters: spaces and '%' from
        # the user name itself never appear raw ('%' only inside %XX
        # escapes), and the encoding reverses to the exact user name.
        stem = p.name
        assert " " not in stem
        import re as _re
        assert _re.fullmatch(r"([A-Za-z0-9._~\-]|%[0-9A-Fa-f]{2})+\.json", stem)
        assert urllib.parse.unquote(stem[:-len(".json")]) == name
        if name == "马威":
            assert name not in stem  # non-ASCII is always percent-encoded
        # The user's folder holds only the uploaded file.
        landing = _landing(users_dir, name)
        assert [q.name for q in landing.iterdir()] == [name + "-f.txt"]


class TestUploadNamedLikeOldIndex:
    async def test_upload_named_localoctop_index_lands_normally(self, users_dir, mgr):
        """'.localoctop-index.json' is no longer a reserved name: a file
        with that name lands under that very name with the uploaded
        content, and the result has no save-aside note."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({".localoctop-index.json": (b"user-content", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", ".localoctop-index.json", b"user-content")
        assert t.state == "done", t.error
        landing = _landing(users_dir, "alice")
        assert (landing / ".localoctop-index.json").read_bytes() == b"user-content"
        assert "saved_as" not in t.result
        assert "saved as" not in mgr.describe(t)
        assert "cloud_path: /localoctop/.localoctop-index.json" in mgr.describe(t)

    async def test_preexisting_old_index_file_untouched(self, users_dir, mgr):
        """A leftover '.localoctop-index.json' from before the move is just
        a user file now: uploading another file must not read, rename or
        modify it (content and mtime unchanged)."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir(parents=True)
        old = landing / ".localoctop-index.json"
        old.write_bytes(b'{"legacy": "index"}')
        os.utime(old, (1000000, 1000000))
        before = (old.read_bytes(), old.stat().st_mtime_ns)
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"data")
        assert t.state == "done", t.error
        assert (old.read_bytes(), old.stat().st_mtime_ns) == before
        assert not list(landing.glob(".localoctop-index.json.foreign-*"))


class TestNonRegularOccupantSavedAside:
    """A final landing name occupied by anything that is not a regular file
    (round 7): the upload must succeed, land under "<name>.uploaded", leave
    the occupant completely untouched, and the result must name both the
    occupant's kind and the file's real cloud_path."""

    async def test_directory_at_landing_name_saved_aside(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir(parents=True)
        (landing / "f.bin").mkdir()
        (landing / "f.bin" / "inner.txt").write_text("inner", "utf-8")
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})

        async def go():
            async with _client_for(mgr) as client:
                return await _push_upload(mgr, client, "alice", "f.bin", b"data")
        t, _ = await asyncio.wait_for(go(), timeout=2.0)
        assert t.state == "done", t.error
        assert (landing / "f.bin.uploaded").read_bytes() == b"data"
        # The directory and its content are exactly as they were.
        assert (landing / "f.bin").is_dir()
        assert (landing / "f.bin" / "inner.txt").read_text("utf-8") == "inner"
        desc = mgr.describe(t)
        assert "directory" in desc and "f.bin.uploaded" in desc
        assert "cloud_path: /localoctop/f.bin.uploaded" in desc

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
    async def test_symlink_at_landing_name_saved_aside(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        landing = home / "localoctop"
        landing.mkdir()
        other = home / "somewhere-else"
        other.mkdir()
        target = other / "real.txt"
        target.write_bytes(b"original-target")
        os.symlink(target, landing / "f.bin")
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})

        async def go():
            async with _client_for(mgr) as client:
                return await _push_upload(mgr, client, "alice", "f.bin", b"data")
        t, _ = await asyncio.wait_for(go(), timeout=2.0)
        assert t.state == "done", t.error
        assert (landing / "f.bin.uploaded").read_bytes() == b"data"
        # The link itself is untouched, still a link, same target; the file
        # it points at was never written through.
        assert (landing / "f.bin").is_symlink()
        assert os.readlink(landing / "f.bin") == str(target)
        assert target.read_bytes() == b"original-target"
        desc = mgr.describe(t)
        assert "symbolic link" in desc and "f.bin.uploaded" in desc
        assert "cloud_path: /localoctop/f.bin.uploaded" in desc

    async def test_saved_aside_rupload_never_deletes_occupant(self, users_dir, mgr):
        """After a save-aside upload, re-uploading the same local file —
        content unchanged and content changed — must never remove the
        occupant and must never remove the saved-aside copy either."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir(parents=True)
        (landing / "f.bin").mkdir()
        mgr.registry = FakeRegistry({"f.bin": (b"v1", 1000)})

        async def go(content: bytes):
            async with _client_for(mgr) as client:
                return await _push_upload(mgr, client, "alice", "f.bin", content)
        t1, _ = await asyncio.wait_for(go(b"v1"), timeout=2.0)
        assert t1.state == "done", t1.error
        assert (landing / "f.bin").is_dir()
        assert (landing / "f.bin.uploaded").read_bytes() == b"v1"
        # transfer_status on the finished transfer (what the agent sees when
        # it queries by id later) must also point at the real location.
        assert "cloud_path: /localoctop/f.bin.uploaded" in mgr.describe(t1)

        # Same content again: the index records the saved-aside name's
        # landed stats, so "already up to date" fires against the copy
        # actually on disk — and nothing is removed. The rendered
        # cloud_path must be the file's real location, not the original
        # name (which the directory still occupies).
        t2 = await asyncio.wait_for(mgr.start_upload("alice", "pair-1", "f.bin"), timeout=2.0)
        assert t2.state == "done" and t2.result.get("fresh") is True
        desc2 = mgr.describe(t2)
        assert "cloud_path: /localoctop/f.bin.uploaded" in desc2
        assert "cloud_path: /localoctop/f.bin\n" not in desc2
        assert "f.bin.uploaded" in desc2 and "directory" in desc2
        assert (landing / "f.bin").is_dir()
        assert (landing / "f.bin.uploaded").read_bytes() == b"v1"
        assert not list(landing.glob("f.bin.uploaded-*"))

        # Changed content: the occupant is not a regular file, so it is
        # never moved/deleted; the new copy lands on a suffixed name.
        mgr.registry = FakeRegistry({"f.bin": (b"v2-changed", 2000)})
        t3, _ = await asyncio.wait_for(go(b"v2-changed"), timeout=2.0)
        assert t3.state == "done", t3.error
        assert (landing / "f.bin").is_dir()
        assert (landing / "f.bin.uploaded").read_bytes() == b"v1"
        assert (landing / "f.bin.uploaded-1").read_bytes() == b"v2-changed"


class TestHashRaceDefence:
    async def test_file_changed_mid_hash_is_kept(self, users_dir, mgr, monkeypatch):
        """The occupant is rewritten WHILE its sha256 is being computed:
        the before/after fstat must catch it and the copy must survive
        (round 6, item 4)."""
        import localoctop.transfers as tr
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"doc.txt": (b"v1", 1000)})
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1")
            assert t1.state == "done"
            mgr.registry = FakeRegistry({"doc.txt": (b"v2", 2000)})
            dest = users_dir / "alice" / "localoctop" / "doc.txt"
            real = tr._sha256_fd_own
            state = {"done": False}

            def racing(fd):
                if not state["done"]:
                    state["done"] = True
                    dest.write_bytes(b"edited-mid-hash")
                return real(fd)

            monkeypatch.setattr(tr, "_sha256_fd_own", racing)
            t2, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v2")
        assert t2.state == "done", t2.error
        landing = dest.parent
        kept = [p for p in landing.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1
        assert kept[0].read_bytes() == b"edited-mid-hash"
        assert dest.read_bytes() == b"v2"


class TestThreadsAndHandles:
    async def test_cancelled_hash_thread_cannot_touch_recycled_fd(self, users_dir, mgr, monkeypatch):
        """A slow hashing thread outlives its cancelled coroutine and the
        transfer's own handle cleanup; because the thread works on a
        duplicated fd it owns, the next transfer (which recycles the fd
        number) is never read or written by it (round 6, item 5)."""
        import localoctop.transfers as tr
        import threading
        home = _mk_user(users_dir, "alice")
        (home / "one.txt").write_bytes(b"first-file")
        real = tr._sha256_fd_own
        entered = threading.Event()
        release = threading.Event()
        state = {"patch": True}

        def slow(fd):
            if state["patch"]:
                entered.set()
                release.wait(5.0)
            return real(fd)

        monkeypatch.setattr(tr, "_sha256_fd_own", slow)
        task = asyncio.create_task(mgr.start_download("alice", "pair-1", "/one.txt", "", False))
        # Wait until the worker thread is inside the slow hash.
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 2.0), 3.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The transfer record finished and released its own handles.
        for t in mgr._transfers.values():
            assert t.state != "active"
            assert t.file_fd == -1
        # A new transfer recycles fd numbers; the slow thread must not
        # interfere with it.
        state["patch"] = False
        (home / "two.txt").write_bytes(b"second-file")
        t2 = await mgr.start_download("alice", "pair-1", "/two.txt", "", False)
        assert t2.expected_sha == hashlib.sha256(b"second-file").hexdigest()
        t2.finish(True)
        assert (home / "two.txt").read_bytes() == b"second-file"
        # Let the slow thread finish; it must not raise into the void.
        release.set()
        await asyncio.sleep(0.2)

    async def test_aclose_fails_inflight_transfers_and_releases_handles(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        home = users_dir / "alice"
        (home / "out.txt").write_bytes(b"payload")
        mgr.registry = FakeRegistry({"up.bin": (b"up-data", 1000)})
        before = len(os.listdir("/proc/self/fd"))
        t_up = await mgr.start_upload("alice", "pair-1", "up.bin")
        t_down = await mgr.start_download("alice", "pair-1", "/out.txt", "", False)
        assert t_up.state == "active" and t_down.state == "active"
        await mgr.aclose()
        assert t_up.state == "failed" and "shutting down" in t_up.error
        assert t_down.state == "failed" and "shutting down" in t_down.error
        assert t_up.file_fd == -1 and t_down.file_fd == -1
        assert len(os.listdir("/proc/self/fd")) <= before


class TestCompleteRobustness:
    async def test_ledger_save_failure_still_succeeds_and_idempotent(self, users_dir, mgr, tmp_path):
        """Landing succeeded but the ledger cannot be written (the ledger
        directory was replaced by a plain file): the transfer is done, the
        file is in place, the request does not error, handles are released,
        and a retried complete returns the same state without touching the
        landed file."""
        _mk_user(users_dir, "alice")
        # Sabotage the ledger home AFTER construction: nothing can be
        # written into a path that is a plain file.
        os.rmdir(mgr.ledger_dir)
        blocker = mgr.ledger_dir
        blocker.write_bytes(b"not-a-directory")
        mgr.registry = FakeRegistry({"f.bin": (b"data", 1000)})
        before = len(os.listdir("/proc/self/fd"))
        async with _client_for(mgr) as client:
            t, resp = await _push_upload(mgr, client, "alice", "f.bin", b"data")
            assert resp.status_code == 200
            assert t.state == "done", t.error
            dest = _landing(users_dir, "alice") / "f.bin"
            assert dest.read_bytes() == b"data"
            st1 = dest.stat()
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
            r2 = await client.post(f"/mcp/localoctop/transfer/{t.id}/complete",
                                   json={"ok": True, "size": 4,
                                         "sha256": hashlib.sha256(b"data").hexdigest()},
                                   headers=auth)
            assert r2.status_code == 200 and r2.json()["state"] == "done"
            st2 = dest.stat()
            assert (st1.st_ino, st1.st_mtime_ns) == (st2.st_ino, st2.st_mtime_ns)
        assert len(os.listdir("/proc/self/fd")) <= before


class TestLandingRetries:
    async def test_occupant_moved_away_during_landing_retries(self, users_dir, mgr):
        """The occupant vanishes between the EEXIST and the keep-aside
        (somebody else moved it first): _land retries and succeeds."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / "doc.txt").write_bytes(b"agent-made")
        mgr.registry = FakeRegistry({"doc.txt": (b"fresh", 1000)})
        real_keep = mgr._keep_cloud_edited
        state = {"done": False}

        def whisk_away(dirs, name):
            if not state["done"]:
                state["done"] = True
                os.rename(landing / name, landing / "whisked-away.txt")
            return real_keep(dirs, name)

        mgr._keep_cloud_edited = whisk_away
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"fresh")
        assert t.state == "done", t.error
        assert (landing / "doc.txt").read_bytes() == b"fresh"
        assert (landing / "whisked-away.txt").read_bytes() == b"agent-made"

    async def test_two_conflicts_report_both_kept_names(self, users_dir, mgr):
        """Two successive conflicts produce two kept-aside copies and the
        result text names BOTH (round 6, item 8)."""
        _mk_user(users_dir, "alice")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / "doc.txt").write_bytes(b"occupant-one")
        mgr.registry = FakeRegistry({"doc.txt": (b"fresh", 1000)})
        real_keep = mgr._keep_cloud_edited
        state = {"n": 0}

        def replant(dirs, name):
            kept = real_keep(dirs, name)
            state["n"] += 1
            if state["n"] == 1:
                # A new occupant appears right after the first was moved.
                (landing / name).write_bytes(b"occupant-two")
            return kept

        mgr._keep_cloud_edited = replant
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"fresh")
        assert t.state == "done", t.error
        assert len(t.result.get("kept_as_list") or []) == 2
        text = mgr.describe(t)
        for kept in t.result["kept_as_list"]:
            assert kept in text
        kept_files = sorted(p.read_bytes() for p in landing.iterdir()
                            if ".cloud-edited-" in p.name)
        assert kept_files == [b"occupant-one", b"occupant-two"]
        assert (landing / "doc.txt").read_bytes() == b"fresh"


class TestLandingSubdirValidation:
    @pytest.mark.parametrize("bad", ["a/b", "..", "", ".", "a\\b", "x\x01y"])
    def test_invalid_subdir_refused_at_startup(self, users_dir, monkeypatch, bad):
        monkeypatch.setenv("LOCALOCTOP_LANDING_SUBDIR", bad)
        with pytest.raises(ValueError) as ei:
            TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        assert "LOCALOCTOP_LANDING_SUBDIR" in str(ei.value)

    def test_valid_subdir_accepted(self, users_dir, monkeypatch):
        monkeypatch.setenv("LOCALOCTOP_LANDING_SUBDIR", "incoming.files")
        m = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        assert m.landing_subdir == "incoming.files"


# --------------------------------------------------- LOX-002 T1: the ledger

class TestLedgerMovedToStateDir:
    async def test_ledger_gone_means_reupload_and_previous_copy_kept(self, users_dir, mgr):
        """Deleting the ledger file while the cloud copy sits untouched:
        the record is gone, so the file is re-sent, and the previous cloud
        copy (now unprovable as ours) is renamed aside — never deleted."""
        _mk_user(users_dir, "alice")
        content = b"same-bytes"
        mgr.registry = FakeRegistry({"report.xlsx": (content, 4242)})
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "report.xlsx", content)
            assert t1.state == "done"
            dest = users_dir / "alice" / "localoctop" / "report.xlsx"
            assert dest.read_bytes() == content
            # The ledger vanishes (lost, corrupted, tokens rotated — all the
            # same to the adapter).
            os.unlink(_ledger_path(mgr, "alice"))
            t2, _ = await _push_upload(mgr, client, "alice", "report.xlsx", content)
        assert t2.state == "done" and not t2.result.get("fresh")
        assert dest.read_bytes() == content
        kept = [p for p in dest.parent.iterdir() if ".cloud-edited-" in p.name]
        assert len(kept) == 1 and kept[0].read_bytes() == content

    async def test_cloud_copy_removed_or_moved_reuploads_to_original_name(self, users_dir, mgr):
        """The ledger still records the file but the user deleted it in the
        cloud (or moved it away): treated as 'not fetched' — re-sent and
        landed under the original name."""
        _mk_user(users_dir, "alice")
        content = b"data"
        mgr.registry = FakeRegistry({"f.txt": (content, 1000)})
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "f.txt", content)
            assert t1.state == "done"
            landing = _landing(users_dir, "alice")
            os.unlink(landing / "f.txt")
            t2, _ = await _push_upload(mgr, client, "alice", "f.txt", content)
            assert t2.state == "done" and not t2.result.get("fresh")
            assert (landing / "f.txt").read_bytes() == content
            assert t2.result["visible"] == "/localoctop/f.txt"
            # Moved away instead of deleted: same outcome.
            os.rename(landing / "f.txt", landing / "f-moved-away.txt")
            t3, _ = await _push_upload(mgr, client, "alice", "f.txt", content)
        assert t3.state == "done" and not t3.result.get("fresh")
        assert (landing / "f.txt").read_bytes() == content
        assert t3.result["visible"] == "/localoctop/f.txt"
        assert (landing / "f-moved-away.txt").read_bytes() == content

    async def test_concurrent_uploads_both_recorded(self, users_dir, mgr):
        """Two transfers of the same user finishing at the same time: the
        per-user ledger lock must serialise read-modify-write so both
        records survive."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"a.bin": (b"aaa", 1000), "b.bin": (b"bbb", 1000)})
        async with _client_for(mgr) as client:
            ta = await mgr.start_upload("alice", "pair-1", "a.bin")
            tb = await mgr.start_upload("alice", "pair-1", "b.bin")
            auth = {"Authorization": "Bearer btok-alice"}
            for t, payload in ((ta, b"aaa"), (tb, b"bbb")):
                h = {**auth, "X-Transfer-Ticket": t.ticket}
                r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                                     content=payload, headers=h)
                assert r.status_code == 200, r.text
            async def finish(t, payload):
                h = {**auth, "X-Transfer-Ticket": t.ticket}
                r = await client.post(f"/mcp/localoctop/transfer/{t.id}/complete",
                                      json={"ok": True, "size": len(payload),
                                            "sha256": hashlib.sha256(payload).hexdigest()},
                                      headers=h)
                assert r.status_code == 200
            await asyncio.gather(finish(ta, b"aaa"), finish(tb, b"bbb"))
        assert ta.state == "done" and tb.state == "done"
        ledger = _read_ledger(mgr, "alice")
        assert "pair-1|a.bin" in ledger and "pair-1|b.bin" in ledger

    async def test_two_pairs_same_user_same_local_path_two_records(self, users_dir, mgr):
        """One human, two machines (two token pairs) fetching a file at the
        same local path: the ledger key carries the pair, so both records
        coexist."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"doc.txt": (b"v1", 1000)})
        async with _client_for(mgr) as client:
            t1, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1")
            assert t1.state == "done"
            t2 = await mgr.start_upload("alice", "pair-2", "doc.txt")
            assert t2.state == "active"  # no fresh shortcut across pairs
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t2.ticket}
            r = await client.put(f"/mcp/localoctop/transfer/{t2.id}/chunk?offset=0",
                                 content=b"v1", headers=auth)
            assert r.status_code == 200, r.text
            r = await client.post(f"/mcp/localoctop/transfer/{t2.id}/complete",
                                  json={"ok": True, "size": 2,
                                        "sha256": hashlib.sha256(b"v1").hexdigest()},
                                  headers=auth)
            assert r.status_code == 200
        assert t2.state == "done", t2.error
        ledger = _read_ledger(mgr, "alice")
        assert "pair-1|doc.txt" in ledger and "pair-2|doc.txt" in ledger


class TestStateDirStartup:
    def test_unwritable_state_dir_refused_at_startup(self, tmp_path, monkeypatch):
        """LOCALOCTOP_STATE_DIR pointing at a place that cannot be created
        or written is a hard startup error — never a silent degradation."""
        blocker = tmp_path / "a-plain-file"
        blocker.write_bytes(b"x")
        monkeypatch.setenv("LOCALOCTOP_STATE_DIR", str(blocker / "state"))
        with pytest.raises(ValueError) as ei:
            TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        assert "LOCALOCTOP_STATE_DIR" in str(ei.value)

    def test_state_dir_created_with_0700(self, tmp_path, monkeypatch):
        import stat as statmod
        monkeypatch.setenv("LOCALOCTOP_STATE_DIR", str(tmp_path / "brand-new-state"))
        m = TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        assert statmod.S_IMODE(m.state_dir.stat().st_mode) == 0o700
        assert statmod.S_IMODE(m.ledger_dir.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# LOX-002 T2: last-resort net on the bridge-facing transfer HTTP routes —
# an exception the route did not anticipate must end the transfer as FAILED
# (plain reason + reference id, handles released), never leave it "active".
# ---------------------------------------------------------------------------

class TestTransferRouteSafetyNet:
    @staticmethod
    def _fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    async def test_chunk_route_unexpected_error_fails_transfer(self, users_dir, mgr, monkeypatch, caplog):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.txt": (b"hello world", 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.txt")
        assert t.state == "active"
        before = self._fds()

        def boom(fd, data, offset):
            raise RuntimeError("something nobody planned for")
        monkeypatch.setattr("localoctop.transfers._pwrite_own", boom)

        client = _client_for(mgr)
        async with client:
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
            with caplog.at_level("ERROR", logger="localoctop.transfers"):
                r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                                     content=b"hello world", headers=auth)
            assert r.status_code == 500
        # The transfer is FAILED with a plain reason + reference — not active.
        assert t.state == "failed"
        assert "internal problem" in t.error
        assert "something nobody planned for" not in t.error
        assert t.file_fd < 0 and t.dirs is None  # handles released
        assert self._fds() <= before
        # transfer_status now answers FAILED in plain language, not IN PROGRESS.
        text = mgr.describe(t)
        assert text.startswith("FAILED")
        assert "internal problem" in text
        # The log carries the same reference id and the real traceback.
        import re as _re
        m = _re.search(r"\b[0-9a-f]{8}\b", t.error)
        assert m
        assert any(m.group(0) in rec.getMessage() and rec.exc_info
                   for rec in caplog.records)

    async def test_complete_route_unexpected_error_fails_transfer(self, users_dir, mgr, monkeypatch, caplog):
        _mk_user(users_dir, "alice")
        content = b"hello world"
        mgr.registry = FakeRegistry({"f.txt": (content, 1000)})
        t = await mgr.start_upload("alice", "pair-1", "f.txt")
        client = _client_for(mgr)
        auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
        base = f"/mcp/localoctop/transfer/{t.id}"
        before = self._fds()
        async with client:
            r = await client.put(f"{base}/chunk?offset=0", content=content, headers=auth)
            assert r.status_code == 200

            async def boom(transfer, digest):
                raise RuntimeError("landing blew up unexpectedly")
            monkeypatch.setattr(mgr, "_land", boom)
            sha = hashlib.sha256(content).hexdigest()
            with caplog.at_level("ERROR", logger="localoctop.transfers"):
                r = await client.post(f"{base}/complete",
                                      json={"ok": True, "size": len(content), "sha256": sha},
                                      headers=auth)
            assert r.status_code == 500
        assert t.state == "failed"
        assert "internal problem" in t.error
        assert "landing blew up unexpectedly" not in t.error
        assert t.file_fd < 0 and t.dirs is None
        assert self._fds() <= before

    async def test_content_route_unexpected_error_fails_transfer(self, users_dir, mgr, monkeypatch, caplog):
        _mk_user(users_dir, "alice")
        src = users_dir / "alice" / "doc.txt"
        src.write_bytes(b"serve me")
        mgr.registry = FakeRegistry()
        t = await mgr.start_download("alice", "pair-1", "/doc.txt", "out.txt", False)
        assert t.state == "active"
        before = self._fds()

        def boom(fd, n, offset):
            raise RuntimeError("read blew up unexpectedly")
        monkeypatch.setattr("localoctop.transfers._pread_own", boom)

        client = _client_for(mgr)
        async with client:
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
            with caplog.at_level("ERROR", logger="localoctop.transfers"):
                # The stream is cut (headers were already sent); what matters
                # is the transfer ends FAILED, never stuck "active".
                try:
                    await client.get(f"/mcp/localoctop/transfer/{t.id}/content", headers=auth)
                except Exception:
                    pass
        assert t.state == "failed"
        assert "internal problem" in t.error
        assert "read blew up unexpectedly" not in t.error
        assert t.file_fd < 0 and t.dirs is None
        assert self._fds() <= before

    async def test_content_route_safety_net_is_the_live_handler(self, users_dir, mgr,
                                                                monkeypatch, caplog):
        """The content route is the one registered handler: its last-resort
        net must be the code that actually runs on an unexpected read
        failure (not a shadowed duplicate). A marker on _fail_unexpected
        proves the net itself fired — a route without the net would leave
        the transfer 'active' and never call it."""
        _mk_user(users_dir, "alice")
        src = users_dir / "alice" / "doc.txt"
        src.write_bytes(b"serve me")
        mgr.registry = FakeRegistry()
        t = await mgr.start_download("alice", "pair-1", "/doc.txt", "out.txt", False)

        fired = []
        real_fail = mgr._fail_unexpected

        def marked_fail(transfer, where, ref):
            fired.append((transfer.id, where, ref))
            return real_fail(transfer, where, ref)
        monkeypatch.setattr(mgr, "_fail_unexpected", marked_fail)

        def boom(fd, n, offset):
            raise RuntimeError("read blew up unexpectedly")
        monkeypatch.setattr("localoctop.transfers._pread_own", boom)

        client = _client_for(mgr)
        async with client:
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
            with caplog.at_level("ERROR", logger="localoctop.transfers"):
                try:
                    await client.get(f"/mcp/localoctop/transfer/{t.id}/content", headers=auth)
                except Exception:
                    pass
        # The live handler's net ran exactly once, for THIS transfer, with a
        # reference id that matches both the transfer error and the log.
        assert len(fired) == 1
        assert fired[0][0] == t.id and fired[0][1] == "sending the file"
        assert t.state == "failed"
        assert fired[0][2] in t.error
        assert any(fired[0][2] in rec.getMessage() and rec.exc_info
                   for rec in caplog.records)

    async def test_content_touch_updates_activity_during_stream(self, users_dir, mgr,
                                                                monkeypatch):
        """A download served in several reads must keep the transfer's
        activity clock moving: every read that produces data advances
        t.updated, so a long send-back is never mistaken for a stalled
        transfer by the background sweep."""
        _mk_user(users_dir, "alice")
        src = users_dir / "alice" / "big.bin"
        # Three reads: 1 MiB + 1 MiB + a tail (the route reads 1 MiB a time).
        payload = os.urandom(1024 * 1024 * 2 + 123)
        src.write_bytes(payload)
        mgr.registry = FakeRegistry()
        t = await mgr.start_download("alice", "pair-1", "/big.bin", "out.bin", False)
        assert t.state == "active"

        # Freeze the clock: each _pread_own return bumps monotonic by a
        # second, so a touch() after every read is the ONLY thing that can
        # keep `updated` current — a missing touch shows up immediately.
        # monkeypatch undoes both patches on teardown; nothing to restore.
        now = [time.monotonic()]
        monkeypatch.setattr("localoctop.transfers.time.monotonic",
                            lambda: now[0])
        real_pread = transfers_mod._pread_own

        def advancing_pread(fd, n, offset):
            data = real_pread(fd, n, offset)
            now[0] += 1.0  # one second of wall time per read
            return data
        monkeypatch.setattr("localoctop.transfers._pread_own", advancing_pread)

        client = _client_for(mgr)
        async with client:
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
            r = await client.get(f"/mcp/localoctop/transfer/{t.id}/content", headers=auth)
            assert r.status_code == 206
            assert r.content == payload
        # The clock advanced by (number of reads) seconds from `created`;
        # `updated` must have tracked the last read, not stayed at creation.
        assert now[0] - t.created >= 3.0  # at least three reads happened
        assert t.updated > t.created + 2.0
        assert t.updated <= now[0]
        # received tracks the highest byte served.
        assert t.received == len(payload)


# ---------------------------------------------------------------------------
# LOX-002 T3-1: a failure to create the anonymous temp file AFTER the
# transfer was registered must fail the transfer on the spot (plain reason,
# handles closed exactly once) — not strand it "active" for the sweep.
# ---------------------------------------------------------------------------

class TestStartUploadTmpfileFailure:
    @staticmethod
    def _fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    @pytest.mark.parametrize("err", [errno.ENOSPC, errno.EOPNOTSUPP])
    async def test_tmpfile_open_failure_fails_transfer_once(self, users_dir, mgr, monkeypatch, err):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.txt": (b"data", 1000)})
        real_open = os.open

        def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
            # O_TMPFILE itself includes the O_DIRECTORY bit, so test for the
            # full flag set, not a single overlapping bit.
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(err, os.strerror(err))
            return real_open(path, flags, mode, dir_fd=dir_fd)
        monkeypatch.setattr(os, "open", guarded_open)

        before = self._fds()
        t = await mgr.start_upload("alice", "pair-1", "f.txt")
        # This very call reports the failure with the system's reason.
        assert t.state == "failed"
        assert "temporary file could not be created" in t.error
        assert os.strerror(err) in t.error
        assert t.file_fd < 0 and t.dirs is None
        # A background sweep neither raises nor closes anything twice: the fd
        # count is unchanged, and a concurrent healthy transfer still works.
        await mgr.sweep_transfers()
        assert self._fds() <= before

        monkeypatch.undo()  # restore os.open for the healthy transfer
        _mk_user(users_dir, "bob")
        mgr.registry = FakeRegistry({"g.txt": (b"ok", 1000)})
        t2 = await mgr.start_upload("bob", "pair-1", "g.txt")
        assert t2.state == "active"
        t2.finish(True)


# ---------------------------------------------------------------------------
# LOX-002 T3-2: a short pwrite must be retried until the whole chunk is on
# disk; a write that makes no progress fails the chunk (and the transfer)
# instead of recording a hole as "received".
# ---------------------------------------------------------------------------

class TestShortWriteHandling:
    async def test_partial_then_full_write_lands_byte_identical(self, users_dir, mgr, monkeypatch):
        _mk_user(users_dir, "alice")
        content = os.urandom(100_000)
        mgr.registry = FakeRegistry({"big.bin": (content, 1000)})
        real_pwrite = os.pwrite
        state = {"calls": 0}

        def flaky_pwrite(fd, data, offset):
            state["calls"] += 1
            if state["calls"] == 1:
                return real_pwrite(fd, data[: len(data) // 2], offset)  # half
            return real_pwrite(fd, data, offset)
        monkeypatch.setattr(os, "pwrite", flaky_pwrite)

        client = _client_for(mgr)
        async with client:
            t, resp = await _push_upload(mgr, client, "alice", "big.bin", content)
        assert resp.json()["state"] == "done"
        landed = _landing(users_dir, "alice") / "big.bin"
        assert landed.read_bytes() == content

    async def test_zero_progress_write_fails_never_reports_success(self, users_dir, mgr, monkeypatch):
        _mk_user(users_dir, "alice")
        content = b"x" * 10_000
        mgr.registry = FakeRegistry({"z.bin": (content, 1000)})

        def stuck_pwrite(fd, data, offset):
            return 0
        monkeypatch.setattr(os, "pwrite", stuck_pwrite)

        t = await mgr.start_upload("alice", "pair-1", "z.bin")
        client = _client_for(mgr)
        async with client:
            auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
            r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                                 content=content, headers=auth)
            assert r.status_code == 507
        assert t.state == "failed"
        assert "could not write" in t.error
        assert t.received == 0
        assert not (_landing(users_dir, "alice") / "z.bin").exists()


# ---------------------------------------------------------------------------
# LOX-002 T4 requirement 1: while a completion flow is still going to use its
# dir handles, neither the background sweep nor aclose() may close them — a
# closed fd number can be handed straight to ANOTHER user's transfer, and the
# resumed flow would then operate on that user's folder.
# ---------------------------------------------------------------------------

class TestT4CompletionFlowKeepsItsHandles:
    @staticmethod
    def _fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    @staticmethod
    def _stall_now(monkeypatch):
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "STALL_SECONDS", 0.01)

    async def _first_upload(self, mgr, users_dir):
        """Land doc.txt once so the ledger records our copy; the next landing
        then has an occupant to move aside, which is the path that hashes on
        a worker thread."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"doc.txt": (b"v1-old-copy", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "doc.txt", b"v1-old-copy")
        assert t.state == "done"
        return t

    async def _second_upload_stuck_in_hash(self, mgr, users_dir, monkeypatch):
        """Start alice's SECOND upload of doc.txt and let its completion flow
        stop inside the moved-aside hashing thread. Returns (client, task, t,
        release_event) where the task is the HTTP complete request driving the
        flow (the flow, and thus t.lock and t.dirs, is in use until release is
        set)."""
        import localoctop.transfers as tr
        release = threading.Event()
        entered = threading.Event()
        real = tr._sha256_fd_own

        def slow(fd):
            entered.set()
            release.wait(10.0)
            return real(fd)

        monkeypatch.setattr(tr, "_sha256_fd_own", slow)
        content = b"v2-fresh-content"
        mgr.registry = FakeRegistry({"doc.txt": (content, 2000)})
        client = _client_for(mgr)
        t = await mgr.start_upload("alice", "pair-1", "doc.txt")
        assert t.state == "active"
        auth = {"Authorization": "Bearer btok-alice", "X-Transfer-Ticket": t.ticket}
        r = await client.put(f"/mcp/localoctop/transfer/{t.id}/chunk?offset=0",
                             content=content, headers=auth)
        assert r.status_code == 200
        task = asyncio.create_task(client.post(
            f"/mcp/localoctop/transfer/{t.id}/complete",
            json={"ok": True, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()},
            headers=auth))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5.0), 6.0)
        return client, task, t, release

    async def test_sweep_skips_transfer_whose_completion_is_in_flight(
            self, users_dir, mgr, monkeypatch):
        await self._first_upload(mgr, users_dir)
        before = self._fds()
        client, task, t, release = await self._second_upload_stuck_in_hash(
            mgr, users_dir, monkeypatch)
        try:
            self._stall_now(monkeypatch)
            t.updated = time.monotonic() - 10
            await mgr.sweep_transfers()
            # The flow is mid-landing: its handles must still be open and the
            # transfer must not have been aborted under it.
            assert t.state == "active"
            assert t.dirs is not None and t.file_fd >= 0
            release.set()
            resp = await task
            assert resp.status_code == 200 and t.state == "done"
            assert (_landing(users_dir, "alice") / "doc.txt").read_bytes() == b"v2-fresh-content"
        finally:
            release.set()
            await client.aclose()
        assert self._fds() <= before

    async def test_aclose_waits_then_cancels_completion_and_frees_handles(
            self, users_dir, mgr, monkeypatch):
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "CLOSE_WAIT_SECONDS", 0.3)
        await self._first_upload(mgr, users_dir)
        before = self._fds()
        client, task, t, release = await self._second_upload_stuck_in_hash(
            mgr, users_dir, monkeypatch)
        try:
            await asyncio.wait_for(mgr.aclose(), timeout=10.0)
            assert t.state == "failed" and "shutting down" in t.error
            assert t.file_fd == -1 and t.dirs is None
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.sleep(0.3)  # let the orphaned hashing thread finish
            await client.aclose()
        assert self._fds() <= before

    async def test_no_cross_talk_when_another_user_takes_the_fd_numbers(
            self, users_dir, mgr, monkeypatch):
        """alice's flow is stuck and declared dead by the sweep; bob then
        starts a transfer and gets fresh handles. If alice's handles had been
        closed, the resumed flow would operate on bob's folder. Bob's folder
        must be untouched and his own transfer must complete normally."""
        await self._first_upload(mgr, users_dir)
        _mk_user(users_dir, "bob")
        client, task, t, release = await self._second_upload_stuck_in_hash(
            mgr, users_dir, monkeypatch)
        try:
            self._stall_now(monkeypatch)
            t.updated = time.monotonic() - 10
            await mgr.sweep_transfers()
            assert t.state == "active"  # held lock: skipped, not aborted

            bob_content = b"bob-little-file"
            mgr.registry = FakeRegistry({"b0.bin": (bob_content, 1000)})
            tb, _ = await _push_upload(mgr, client, "bob", "b0.bin", bob_content)
            assert tb.state == "done"
            bob_files = sorted(p.name for p in _landing(users_dir, "bob").iterdir())

            release.set()
            resp = await task
            assert resp.status_code == 200 and t.state == "done"
            assert (_landing(users_dir, "alice") / "doc.txt").read_bytes() == b"v2-fresh-content"
            assert sorted(p.name for p in _landing(users_dir, "bob").iterdir()) == bob_files == ["b0.bin"]
            assert (_landing(users_dir, "bob") / "b0.bin").read_bytes() == bob_content
        finally:
            release.set()
            await client.aclose()

    async def test_sweep_does_not_block_on_a_locked_transfer(
            self, users_dir, mgr, monkeypatch):
        """A transfer whose lock is held is skipped, and the scan still
        finishes — it never waits on that transfer."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"a.bin": (b"aaa", 1000), "b.bin": (b"bbb", 1000)})
        t_locked = await mgr.start_upload("alice", "pair-1", "a.bin")
        t_stale = await mgr.start_upload("alice", "pair-1", "b.bin")
        assert t_locked.state == "active" and t_stale.state == "active"
        await t_locked.lock.acquire()  # a completion flow of its own, held
        try:
            self._stall_now(monkeypatch)
            t_stale.updated = time.monotonic() - 10
            started = time.monotonic()
            await asyncio.wait_for(mgr.sweep_transfers(), timeout=1.0)
            elapsed = time.monotonic() - started
        finally:
            t_locked.lock.release()
        assert t_locked.state == "active" and t_locked.dirs is not None
        assert t_stale.state == "failed"
        assert elapsed < 1.0
        t_locked.finish(True)


# ---------------------------------------------------------------------------
# LOX-002 T4 requirement 2: every fd dup'd for a worker thread must be closed
# exactly once — including when the awaiting coroutine is cancelled while the
# work item is still queued and the thread function never runs.
# ---------------------------------------------------------------------------

class TestT4QueuedDupOwnership:
    @staticmethod
    def _fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    @staticmethod
    def _default_workers(loop) -> int:
        return min(32, (os.cpu_count() or 1) + 4)

    async def test_cancelled_while_queued_closes_the_dup_exactly_once(
            self, users_dir, mgr):
        """Fill the thread pool, queue one more dup-then-thread operation, and
        cancel it while queued. When the pool drains, the fd count is back and
        the never-started thread function touched nothing."""
        home = _mk_user(users_dir, "alice")
        (home / "one.txt").write_bytes(b"first-file")
        await asyncio.to_thread(lambda: None)  # ensure the default executor exists
        loop = asyncio.get_running_loop()
        workers = self._default_workers(loop)

        release = threading.Event()
        all_started = asyncio.Event()
        count = {"running": 0}

        def block():
            count["running"] += 1
            if count["running"] == workers:
                loop.call_soon_threadsafe(all_started.set)
            release.wait(10.0)

        blockers = [asyncio.ensure_future(asyncio.to_thread(block)) for _ in range(workers)]
        try:
            # All workers are busy (do NOT wait from inside the pool: it is
            # full, so a pool task waiting on a signal would deadlock).
            await asyncio.wait_for(all_started.wait(), 5.0)
            before = self._fds()
            task = asyncio.create_task(
                mgr.start_download("alice", "pair-1", "/one.txt", "", False))
            await asyncio.sleep(0.1)  # it is now queued behind the blockers
            assert not task.done()  # proving it never got a worker
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            for b in blockers:
                await b
            await asyncio.sleep(0.2)  # the orphaned work item, if run, must no-op
            assert self._fds() <= before
        finally:
            release.set()
            await asyncio.gather(*blockers, return_exceptions=True)

    async def test_cancelled_after_the_thread_started_is_not_double_closed(
            self, users_dir, mgr, monkeypatch):
        """The worker is already inside the hash when the coroutine is
        cancelled: it closes its own dup, the coroutine does not, and no fd is
        closed twice."""
        import localoctop.transfers as tr
        home = _mk_user(users_dir, "alice")
        (home / "one.txt").write_bytes(b"first-file")
        real = tr._sha256_fd_own
        entered = threading.Event()
        release = threading.Event()

        def slow(fd):
            entered.set()
            release.wait(10.0)
            return real(fd)

        monkeypatch.setattr(tr, "_sha256_fd_own", slow)
        before = self._fds()
        task = asyncio.create_task(mgr.start_download("alice", "pair-1", "/one.txt", "", False))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5.0), 6.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.3)
        assert self._fds() <= before

    async def test_run_owning_fd_normal_path_closes_exactly_once(self, users_dir):
        """The helper's happy path: the thread closes the dup it was given."""
        import localoctop.transfers as tr
        home = _mk_user(users_dir, "alice")
        src = home / "h.bin"
        src.write_bytes(b"hash-me")
        fd = os.open(src, os.O_RDONLY)
        try:
            before = self._fds()
            digest = await tr._run_owning_fd(tr._sha256_fd_own, os.dup(fd))
            assert digest == hashlib.sha256(b"hash-me").hexdigest()
            assert self._fds() <= before
        finally:
            os.close(fd)
