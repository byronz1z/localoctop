"""Tests for the out-of-band transfer manager (LOX-002, round 2):
handle-anchored confinement. The user's own folder is hostile ground —
every name in it can be swapped or turned into a symlink at any moment — so
these tests attack the adapter mid-transfer and assert that nothing outside
the user's folder is ever read, written, renamed or deleted.

The bridge side is faked: a stub registry answers transfer_upload /
transfer_download like the Go bridge would, and chunks are pushed through
the real FastAPI routes via httpx. All file operations run for real in the
Linux test container; nothing about the filesystem is mocked.
"""

from __future__ import annotations

import hashlib
import json
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
    CODE_TOO_LARGE,
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


def _read_index(users_dir, user: str) -> dict:
    p = users_dir / user / "localoctop" / ".localoctop-index.json"
    return json.loads(p.read_text("utf-8"))


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
                                     # Hidden-directory lookalikes.
                                     ".hidden", ".config",
                                     ])
    def test_unsafe_names_rejected(self, mgr, bad):
        with pytest.raises(BridgeProtocolError) as ei:
            mgr._user_root_fd(bad)
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


# ------------------------------------------------------------ index safety

class TestIndexSymlinkSafety:
    @needs_symlink
    async def test_index_and_tmp_preplanted_symlinks_not_written(self, users_dir, mgr):
        """Both the index and a would-be index temp name are symlinks into
        bob's folder before the upload starts. Completing the upload must
        not write a single byte through either link."""
        _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        bob_target = bob / "precious.txt"
        bob_target.write_bytes(b"bob-precious")
        bob_tmp_target = bob / "other.txt"
        bob_tmp_target.write_bytes(b"bob-other")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bob.iterdir()}

        landing = _landing(users_dir, "alice")
        landing.mkdir()
        (landing / ".localoctop-index.json").symlink_to(bob_target)
        # A fixed-looking temp name the old code would have used.
        (landing / ".localoctop-index.json.tmp").symlink_to(bob_tmp_target)

        mgr.registry = FakeRegistry({"f.bin": (b"hello", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"hello")
        assert t.state == "done"
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bob.iterdir()}
        assert after == before
        # The landed file itself is fine.
        assert (landing / "f.bin").read_bytes() == b"hello"

    @needs_symlink
    async def test_index_symlink_reads_as_empty(self, users_dir, mgr):
        """An index that is a symlink to another user's file must read as
        'no index' — never leak its content into fresh-check decisions."""
        _mk_user(users_dir, "alice")
        bob = _mk_user(users_dir, "bob")
        landing = _landing(users_dir, "alice")
        landing.mkdir()
        bob_idx = bob / "idx.json"
        bob_idx.write_text(json.dumps({"pair-1|f.bin": {"size": 5, "mtime": 1000, "sha256": "x"}}))
        (landing / ".localoctop-index.json").symlink_to(bob_idx)

        # Even with matching size/mtime in the planted index, the upload
        # must really run (fresh=False) because the index is unreadable.
        reg = FakeRegistry({"f.bin": (b"hello", 1000)})
        mgr.registry = reg
        (landing / "f.bin").write_bytes(b"hello")
        os.utime(landing / "f.bin", (1000, 1000))
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.bin", b"hello")
        assert t.state == "done" and not t.result.get("fresh")


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
        # No leftover intermediate or partial names of any kind.
        assert not list(dest.parent.glob("*.part-*"))
        assert not [p for p in dest.parent.iterdir()
                    if p.name.startswith(".localoctop-") and p.name != ".localoctop-index.json"]
        idx = _read_index(users_dir, "alice")
        ent = idx["pair-1|big.bin"]
        assert ent["size"] == len(content) and ent["sha256"] == hashlib.sha256(content).hexdigest()
        # The index records the landed copy's size and mtime (ns).
        st = dest.stat()
        assert ent["landed_size"] == st.st_size
        assert ent["landed_mtime_ns"] == st.st_mtime_ns
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
        assert not [p for p in landing.iterdir()
                    if p.name.startswith(".localoctop-") and p.name != ".localoctop-index.json"]

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
            assert not [p for p in landing.rglob("*")
                        if p.name.startswith(".localoctop-") and p.name != ".localoctop-index.json"]
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
    async def test_landed_file_is_0644_index_stays_0600(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry({"f.txt": (b"hello", 1000)})
        async with _client_for(mgr) as client:
            t, _ = await _push_upload(mgr, client, "alice", "f.txt", b"hello")
        assert t.state == "done"
        import stat as statmod
        dest = users_dir / "alice" / "localoctop" / "f.txt"
        assert statmod.S_IMODE(dest.stat().st_mode) == 0o644
        idx = users_dir / "alice" / "localoctop" / ".localoctop-index.json"
        assert statmod.S_IMODE(idx.stat().st_mode) == 0o600


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
        mgr.sweep_transfers()
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
        mgr.sweep_transfers()
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
        mgr.sweep_transfers()
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
