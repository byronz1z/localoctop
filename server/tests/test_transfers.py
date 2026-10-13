"""Tests for the out-of-band transfer manager (LOX-002): per-user landing
inside the Octop user folder, user-name safety, read/write symlink
containment, chunked upload offsets, and integrity verification.

The bridge side is faked: a stub registry answers transfer_upload /
transfer_download like the Go bridge would, and chunks are pushed through
the real FastAPI routes via httpx.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

import httpx
import pytest

from localoctop.errors import (
    BridgeProtocolError,
    CODE_INVALID_PARAMS,
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
)
from localoctop.transfers import TransferManager

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
    final path. Recorded calls let tests assert what was requested."""

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


# ------------------------------------------------------------ user names

class TestUserNameSafety:
    def test_chinese_name_accepted(self, users_dir, mgr):
        _mk_user(users_dir, "马威")
        root = mgr._user_root("马威")
        assert root.name == "马威"

    def test_plain_names_accepted(self, users_dir, mgr):
        _mk_user(users_dir, "ann.a-smith_2")
        assert mgr._user_root("ann.a-smith_2").name == "ann.a-smith_2"

    @pytest.mark.parametrize("bad", ["", "   ", "\t", ".", "..", "a/b", "a\\b",
                                     "ma\x00li", "x\ny", "x\x7fy"])
    def test_unsafe_names_rejected(self, mgr, bad):
        with pytest.raises(BridgeProtocolError) as ei:
            mgr._user_root(bad)
        assert ei.value.code == CODE_INVALID_PARAMS

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
            mgr._user_root("linked")
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
        assert t.cloud_path.read_bytes() == b"payload"
        assert t.size == 7

    async def test_traversal_rejected(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        (home / "f.txt").write_bytes(b"a")
        _mk_user(users_dir, "bob")
        (users_dir / "bob" / "secret.txt").write_bytes(b"bob-secret")
        for raw in ("/../bob/secret.txt", "/..%2f..%2fbob/secret.txt"):
            with pytest.raises(BridgeProtocolError) as ei:
                await mgr.start_download("alice", "pair-1", raw, "", False)
            assert ei.value.code in (CODE_NOT_ALLOWED, CODE_NOT_FOUND)

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
        assert "outside your own folder" in ei.value.message

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
    async def test_existing_symlink_at_destination_refused(self, users_dir, mgr):
        home = _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        (bob / "target.txt").write_bytes(b"bob-file")
        landing = home / "localoctop"
        landing.mkdir()
        (landing / "evil.txt").symlink_to(bob / "target.txt")
        mgr.registry = FakeRegistry({"evil.txt": (b"pwned", 1000)})
        with pytest.raises(BridgeProtocolError) as ei:
            await mgr.start_upload("alice", "pair-1", "evil.txt")
        assert ei.value.code == CODE_NOT_ALLOWED
        assert (bob / "target.txt").read_bytes() == b"bob-file"


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
        # The temporary file is gone and the index remembers this version.
        assert not list(dest.parent.glob("*.part-*"))
        idx = mgr._index_load("alice")
        ent = idx["pair-1|big.bin"]
        assert ent["size"] == len(content) and ent["sha256"] == hashlib.sha256(content).hexdigest()
        assert t.result["sha256"] == ent["sha256"]

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


# ------------------------------------------------------------ housekeeping

class TestHousekeeping:
    def test_orphan_part_files_swept_per_user(self, users_dir, monkeypatch):
        for name in ("alice", "bob"):
            landing = _mk_user(users_dir, name) / "localoctop"
            landing.mkdir()
            (landing / "x.bin.part-dead1").write_bytes(b"half")
            (landing / "keep.bin").write_bytes(b"full")
        TransferManager(_Settings(make_token_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        for name in ("alice", "bob"):
            landing = users_dir / name / "localoctop"
            assert not list(landing.glob("*.part-*"))
            assert (landing / "keep.bin").exists()

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
