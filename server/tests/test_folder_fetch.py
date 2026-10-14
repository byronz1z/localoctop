"""LOX-002 T10 tests: fetching a whole folder in one call.

The bridge side is the same fake used across test_transfers.py, extended
with transfer_manifest and per-file upload starts. The design rule under
test is that a folder fetch reuses the ORDINARY single-file path for every
file — same ledger, same "already up to date" shortcut, same keep-aside
rules — and only adds the surrounding batch: limits first, one file at a
time, a failure never dragging the rest down, an offline machine stopping
the run, live progress, and a result that always fits the model-facing cap.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from localoctop.errors import BridgeProtocolError
from localoctop.transfers import TransferManager
from localoctop.tools import ToolService

from conftest import make_token_store
from test_transfers import (
    FakeRegistry,
    _Settings,
    _client_for,
    _ledger_path,
    _mk_user,
)


def _store():
    return make_token_store()


@pytest.fixture
def users_dir(tmp_path, monkeypatch):
    """Same isolated users/state dirs the other transfer tests use."""
    d = tmp_path / "users"
    d.mkdir()
    monkeypatch.setenv("LOCALOCTOP_USERS_DIR", str(d))
    monkeypatch.setenv("LOCALOCTOP_LANDING_SUBDIR", "localoctop")
    monkeypatch.setenv("LOCALOCTOP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("LOCALOCTOP_MAX_TRANSFER_BYTES", raising=False)
    return d


@pytest.fixture
def mgr(users_dir):
    return TransferManager(_Settings(_store()), FakeRegistry(), "/mcp/localoctop/transfer")


def _mgr(users_dir, monkeypatch, registry):
    """A manager bound to the given fake bridge (the conftest fixture builds
    one with an empty default registry)."""
    mgr = TransferManager(_Settings(_store()), registry, "/mcp/localoctop/transfer")
    return mgr


def _files(*specs):
    """{"a.txt": (b"data", 1000), ...} from ("a.txt", b"data", 1000) tuples."""
    return {name: (content, mtime) for name, content, mtime in specs}


async def _drive_batch(mgr, client, t, user="alice", token="btok-alice", timeout=15.0,
                       stop_after: int | None = None):
    """Land the files of a running folder batch through the real HTTP routes,
    the way the real bridge does, until the batch finishes (or until
    stop_after files have landed, leaving the next one in flight)."""
    auth = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout
    handled: set[str] = set()
    landed = 0
    while t.state == "active" and time.monotonic() < deadline:
        if stop_after is not None and landed >= stop_after:
            return t
        for ft in list(mgr._transfers.values()):
            if (ft.direction != "up" or ft.state != "active"
                    or ft.id in handled or ft.batch is not None):
                continue
            handled.add(ft.id)
            content = mgr.registry.files.get(ft.local_path, (b"", 0))[0]
            base = f"/mcp/localoctop/transfer/{ft.id}"
            hdr = {**auth, "X-Transfer-Ticket": ft.ticket}
            r = await client.put(f"{base}/chunk?offset=0", content=content, headers=hdr)
            assert r.status_code == 200, r.text
            import hashlib
            r = await client.post(f"{base}/complete",
                                  json={"ok": True, "size": len(content),
                                        "sha256": hashlib.sha256(content).hexdigest()},
                                  headers=hdr)
            assert r.status_code == 200, r.text
            landed += 1
            if stop_after is not None and landed >= stop_after:
                return t
        await asyncio.sleep(0.02)
    return t


class TestFolderFetchBasics:
    async def test_three_levels_twelve_files_land_with_layout(self, users_dir, mgr):
        """A three-level folder of 12 files: every one lands at the right
        cloud path with identical content, and the ledger holds 12 records.

        The cloud layout keeps the folder's own name as the top component,
        so it matches where a single fetch of the same file lands."""
        _mk_user(users_dir, "alice")
        specs = []
        expected = {}
        for level in range(1, 4):
            for i in range(4):
                rel = "/".join([f"L{n}" for n in range(1, level + 1)] + [f"f{i} 文件.txt"])
                content = f"level-{level}-file-{i}".encode()
                specs.append((f"合同 2026/{rel}", content, 1000 + level * 10 + i))
                expected[rel] = content
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "合同 2026")
            assert t.state == "active"
            await _drive_batch(mgr, client, t)
        assert t.state == "done", t.error
        landing = users_dir / "alice" / "localoctop" / "合同 2026"
        for rel, content in expected.items():
            p = landing / rel
            assert p.is_file(), f"missing {rel}"
            assert p.read_bytes() == content, f"content mismatch at {rel}"
        ledger = json.loads(_ledger_path(mgr, "alice").read_text("utf-8"))
        records = {k: v for k, v in ledger.items() if not k.startswith("_")}
        assert len(records) == 12
        text = mgr.describe(t)
        assert "12 of 12 file(s) sent" in text
        assert "cloud folder: /localoctop/合同 2026" in text

    async def test_folder_layout_matches_single_file_fetch(self, users_dir, mgr):
        """Fetching 合同/a.pdf as part of the folder must land it at the same
        cloud path as fetching it on its own."""
        _mk_user(users_dir, "alice")
        content = b"same-place"
        reg = FakeRegistry(_files(("合同/a.pdf", content, 1000)))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "合同")
            await _drive_batch(mgr, client, t)
        assert (users_dir / "alice" / "localoctop" / "合同" / "a.pdf").read_bytes() == content
        assert t.result["visible"] == "/localoctop/合同"

    async def test_second_fetch_of_unchanged_folder_resends_nothing(self, users_dir, mgr):
        """Re-fetching the same folder: every file is already up to date, so
        the fake bridge must see no new upload start at all."""
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i}.txt", f"data-{i}".encode(), 1000 + i) for i in range(12)]
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1 = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t1)
            assert t1.state == "done", t1.error
            starts_after_first = reg.upload_count()
            assert starts_after_first == 12
            t2 = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t2)
        assert t2.state == "done"
        assert reg.upload_count() == starts_after_first, "no file may be re-sent"
        text = mgr.describe(t2)
        assert "12 file(s) were already up to date" in text

    async def test_two_changed_files_are_the_only_ones_resent(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i}.txt", f"data-{i}".encode(), 1000 + i) for i in range(12)]
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t1 = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t1)
            assert t1.state == "done", t1.error
            before = reg.upload_count()
            reg.files["box/f3.txt"] = (b"changed-3", 5001)
            reg.files["box/f7.txt"] = (b"changed-7-longer", 5002)
            t2 = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t2)
        assert t2.state == "done"
        assert reg.upload_count() == before + 2
        landing = users_dir / "alice" / "localoctop" / "box"
        assert (landing / "f3.txt").read_bytes() == b"changed-3"
        assert (landing / "f7.txt").read_bytes() == b"changed-7-longer"


class TestFolderFetchFailures:
    async def test_one_file_failure_does_not_stop_the_rest(self, users_dir, mgr):
        """The bridge refuses one file's upload: the other 11 land, the failed
        one is named in the result with its reason, and isError is false."""
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.txt", f"data-{i}".encode(), 1000 + i) for i in range(12)]
        reg = FakeRegistry(_files(*specs), fail_uploads={"box/f05.txt": "the file is locked"})
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t)
        assert t.state == "done", t.error
        landing = users_dir / "alice" / "localoctop" / "box"
        landed = sorted(p.name for p in landing.iterdir())
        assert len(landed) == 11 and "f05.txt" not in landed
        text = mgr.describe(t)
        assert "11 of 12 file(s) sent" in text
        assert "box/f05.txt" in text and "the file is locked" in text
        assert "1 file(s) could not be sent" in text
        assert "call fetch_to_cloud on this same folder again" in text
        # tools.py renders a partly-failed batch as a normal result.
        svc = _tool_service(mgr)
        res = await svc._transfer_tool("alice", "pair-1", "transfer_status",
                                       {"transfer_id": t.id}, "box", _now(), "")
        assert res["isError"] is False

    async def test_bridge_offline_stops_the_whole_batch(self, users_dir, mgr):
        """The machine goes away at the 5th file: the batch stops there, the
        first 4 stay in the cloud, and the remaining files are NOT attempted
        one by one."""
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.txt", f"data-{i}".encode(), 1000 + i) for i in range(12)]
        reg = FakeRegistry(_files(*specs), offline_after=4)
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t)
        assert reg.upload_attempts == 5, \
            f"only the 5th start may be refused, got {reg.upload_attempts} attempts"
        assert t.state == "done", t.error
        landing = users_dir / "alice" / "localoctop" / "box"
        assert sorted(p.name for p in landing.iterdir()) == [f"f{i:02d}.txt" for i in range(4)]
        text = mgr.describe(t)
        assert "4 of 12 file(s) sent" in text
        assert "stopped early" in text
        assert "offline" in text

    async def test_stalled_file_marks_offline_and_stops_the_batch(self, users_dir, mgr,
                                                                 monkeypatch):
        """The realistic offline path: two files land, then the third stops
        moving and the sweep declares it dead (offline). The batch stops
        there — status "partly done" — instead of trying the remaining five
        files one by one."""
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "STALL_SECONDS", 0.05)
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.txt", f"data-{i}".encode(), 1000 + i) for i in range(8)]
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "box")
            try:
                await _drive_batch(mgr, client, t, stop_after=2)
                # File 3 is now in flight and will never get a chunk.
                assert t.state == "active"
                for _ in range(400):
                    if t.state != "active":
                        break
                    for ft in list(mgr._transfers.values()):
                        if ft.state == "active" and ft.batch is None:
                            ft.updated = time.monotonic() - 10  # looks stalled
                    await mgr.sweep_transfers()
                    await asyncio.sleep(0.01)
            finally:
                if t.state == "active":
                    await asyncio.wait_for(mgr.aclose(), timeout=15.0)
        assert t.state == "done", t.error
        assert reg.upload_attempts == 3, \
            f"the batch must stop after the third file stalls, got {reg.upload_attempts} attempts"
        landing = users_dir / "alice" / "localoctop" / "box"
        assert sorted(p.name for p in landing.iterdir()) == ["f00.txt", "f01.txt"]
        text = mgr.describe(t)
        assert "2 of 8 file(s) sent" in text
        assert "stopped early" in text and "offline" in text
        assert t.is_error() is False

    async def test_result_names_twenty_failures_and_counts_the_rest(self, users_dir, mgr):
        """300 files all failing: the result lists 20 with "another 280", and
        the whole text still fits MODEL_RESULT_CAP_BYTES."""
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:03d}.txt", b"x", 1000 + i) for i in range(300)]
        fails = {f"box/f{i:03d}.txt": f"reason number {i} that is quite long indeed" for i in range(300)}
        reg = FakeRegistry(_files(*specs), fail_uploads=fails)
        mgr.registry = reg
        t = await mgr.start_upload_dir("alice", "pair-1", "box")
        await _wait_batch(mgr, t)
        assert t.state == "failed"
        text = mgr.describe(t)
        assert "another 280" in text
        from localoctop.tools import MODEL_RESULT_CAP_BYTES
        assert len(text.encode("utf-8")) <= MODEL_RESULT_CAP_BYTES
        # 20 named + the "another" line, not 300.
        assert text.count("reason number") == 20

    async def test_batch_text_shrinks_to_fit_a_small_cap(self, users_dir, mgr, monkeypatch):
        """When even 20 named failures are too much for the model-facing cap,
        the result lists fewer of them rather than overflowing it."""
        import localoctop.tools as tools_mod
        monkeypatch.setattr(tools_mod, "MODEL_RESULT_CAP_BYTES", 900)
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:03d}.txt", b"x", 1000 + i) for i in range(60)]
        fails = {f"box/f{i:03d}.txt": "a long failure reason " * 6 for i in range(60)}
        reg = FakeRegistry(_files(*specs), fail_uploads=fails)
        mgr.registry = reg
        t = await mgr.start_upload_dir("alice", "pair-1", "box")
        await _wait_batch(mgr, t)
        text = mgr.describe(t)
        assert len(text.encode("utf-8")) <= 900
        # Fewer than 20 names are listed, and the count line is honest.
        assert text.count("a long failure reason") < 20
        assert "60 file(s) could not be sent" in text
        assert "not listed" in text

    async def test_empty_folder_is_a_normal_answer(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        reg = FakeRegistry(dirs={"empty": {"files": []}})
        mgr.registry = reg
        t = await mgr.start_upload_dir("alice", "pair-1", "empty")
        assert t.state == "done"
        assert t.is_error() is False
        text = mgr.describe(t)
        assert "Nothing to send" in text and "empty" in text
        assert reg.upload_count() == 0

    async def test_folder_of_only_skipped_entries(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        reg = FakeRegistry(dirs={"links": {"files": [], "skipped": 3}})
        mgr.registry = reg
        t = await mgr.start_upload_dir("alice", "pair-1", "links")
        assert t.state == "done"
        text = mgr.describe(t)
        assert "no regular files" in text and "3" in text
        assert t.is_error() is False


class TestFolderFetchLimits:
    async def test_over_two_thousand_files_is_normal_and_unsent(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        reg = FakeRegistry(dirs={
            "huge": {"over_limit": True, "total_files": 2001, "total_bytes": 99999,
                     "files": ["f.txt"]},
            "huge/subA": {"files": []},
            "huge/subB": {"files": []},
        }, files={"huge/f.txt": (b"x", 1000)})
        mgr.registry = reg
        t = await mgr.start_upload_dir("alice", "pair-1", "huge")
        assert t.is_error() is False
        text = mgr.describe(t)
        assert "more than 2000" in text
        assert "Nothing was sent" in text
        assert "subA" in text and "subB" in text
        assert reg.upload_count() == 0
        landing = users_dir / "alice" / "localoctop" / "huge"
        assert not landing.exists() or list(landing.iterdir()) == []

    async def test_over_total_size_limit_is_normal_and_unsent(self, users_dir, mgr, monkeypatch):
        monkeypatch.setenv("LOCALOCTOP_MAX_TRANSFER_BYTES", "100")
        mgr2 = TransferManager(_Settings(_store()), FakeRegistry(), "/mcp/localoctop/transfer")
        _mk_user(users_dir, "alice")
        reg = FakeRegistry(_files(("box/a.bin", b"x" * 60, 1000),
                                  ("box/b.bin", b"y" * 60, 1001)),
                           dirs={"box/sub": {"files": []}})
        mgr2.registry = reg
        t = await mgr2.start_upload_dir("alice", "pair-1", "box")
        assert t.is_error() is False
        text = mgr2.describe(t)
        assert "NOT TRANSFERRED" in text and "120" in text and "100" in text
        assert "Nothing was sent" in text
        assert reg.upload_count() == 0


class TestFolderFetchProgress:
    async def test_in_progress_then_status_reports_position(self, users_dir, mgr):
        """A batch that is still running: describe() says which file of how
        many and how much has gone, and transfer_status keeps reporting
        until it finishes."""
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.txt", f"data-{i}".encode(), 1000 + i) for i in range(6)]
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "box")
            # Nothing has been driven yet: the first file is in flight.
            await asyncio.sleep(0.05)
            text = mgr.describe(t)
            assert "IN PROGRESS" in text and "file 1 of 6" in text
            assert "current file: box/f00.txt" in text
            assert f"transfer_id: {t.id}" in text
            await _drive_batch(mgr, client, t)
            text = mgr.describe(t)
            assert "DONE local → cloud (folder)" in text
            assert "6 of 6 file(s) sent" in text

    async def test_fetch_tool_answers_in_progress_then_status_finishes(self, users_dir, mgr,
                                                                       monkeypatch):
        """End to end through the tool layer: fetch_to_cloud on a folder comes
        back IN PROGRESS (files trickle in), transfer_status shows "file n of
        N", and the final poll reports DONE.

        The bridge is slowed by holding each file's chunk until the test lets
        it go, so the batch really is mid-flight when the first answer is
        rendered."""
        import localoctop.tools as tools_mod

        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.txt", f"data-{i}".encode(), 1000 + i) for i in range(6)]
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        svc = _tool_service(mgr)
        async with _client_for(mgr) as client:
            # A short wait budget so the call answers while files remain.
            monkeypatch.setattr(tools_mod, "TRANSFER_WAIT_SECONDS", 0.4)
            res = await svc._transfer_tool("alice", "pair-1", "fetch_to_cloud",
                                           {"path": "box"}, "box", _now(), "")
            assert res["isError"] is False
            first = res["content"][0]["text"]
            assert "IN PROGRESS" in first, first
            assert "of 6" in first
            tid = [x for x in mgr._transfers.values() if x.batch is not None][0].id
            batch_rec = mgr.get_for_user("alice", tid)
            # Nothing has pushed a chunk yet, so the batch is genuinely
            # parked on its first file: the status poll must say so.
            mid = await svc._transfer_tool("alice", "pair-1", "transfer_status",
                                           {"transfer_id": tid}, tid, _now(), "")
            assert mid["isError"] is False
            assert "IN PROGRESS" in mid["content"][0]["text"]
            assert "file 1 of 6" in mid["content"][0]["text"]
            assert "current file: box/f00.txt" in mid["content"][0]["text"]
            assert batch_rec.state == "active"
            # Now push every file through, the way the real bridge would.
            await _drive_batch(mgr, client, batch_rec)
        assert batch_rec.state == "done"
        done = await svc._transfer_tool("alice", "pair-1", "transfer_status",
                                        {"transfer_id": tid}, tid, _now(), "")
        assert done["isError"] is False
        assert "DONE local → cloud (folder)" in done["content"][0]["text"]
        assert "6 of 6 file(s) sent" in done["content"][0]["text"]


class TestFolderFetchLifecycle:
    async def test_sweep_never_aborts_a_live_batch(self, users_dir, mgr, monkeypatch):
        """The stall sweep must not declare a folder fetch dead while its
        driving task is still alive: the task touches the record as it moves
        from file to file, so a read that looks stale is only a slow file."""
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "STALL_SECONDS", 0.01)
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.txt", f"data-{i}".encode(), 1000 + i) for i in range(4)]
        mgr.registry = FakeRegistry(_files(*specs))
        t = await mgr.start_upload_dir("alice", "pair-1", "box")
        try:
            t.updated = time.monotonic() - 10  # looks stale
            await mgr.sweep_transfers()
            assert t.state == "active", "a live batch must not be aborted by the sweep"
        finally:
            await asyncio.wait_for(mgr.aclose(), timeout=15.0)

    async def test_finished_batch_record_expires_like_any_other(self, users_dir, mgr, monkeypatch):
        import localoctop.transfers as tr
        monkeypatch.setattr(tr, "RECORD_TTL_SECONDS", 0.01)
        _mk_user(users_dir, "alice")
        reg = FakeRegistry(_files(("box/a.txt", b"x", 1000)))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t)
        assert t.state == "done"
        assert t.id in mgr._transfers
        t.finished = time.monotonic() - 10
        await mgr.sweep_transfers()
        assert t.id not in mgr._transfers

    async def test_aclose_during_batch_returns_in_time_and_frees_handles(self, users_dir, mgr):
        _mk_user(users_dir, "alice")
        specs = [(f"box/f{i:02d}.bin", os.urandom(64), 1000 + i) for i in range(30)]
        reg = FakeRegistry(_files(*specs))
        mgr.registry = reg
        before = len(os.listdir("/proc/self/fd"))
        t = await mgr.start_upload_dir("alice", "pair-1", "box")
        await asyncio.sleep(0.05)  # let the batch start its first file
        await asyncio.wait_for(mgr.aclose(), timeout=15.0)
        assert t.state != "active", "the batch record must not be left active"
        assert t.batch_task is None or t.batch_task.done()
        actives = [x for x in mgr._transfers.values() if x.state == "active"]
        assert actives == []
        after = len(os.listdir("/proc/self/fd"))
        assert after <= before, f"fd leak: {before} -> {after}"

    async def test_old_bridge_gets_a_normal_explanation(self, users_dir, mgr):
        """An employee running a pre-T10 program that still marks folders but
        has no manifest method: the unknown method must be a normal answer
        naming the update, never a program error."""
        _mk_user(users_dir, "alice")
        mgr.registry = FakeRegistry(dirs={"box": {"files": []}}, manifest_missing=True)
        svc = _tool_service(mgr)
        res = await svc._transfer_tool("alice", "pair-1", "fetch_to_cloud",
                                       {"path": "box"}, "box", _now(), "")
        assert res["isError"] is False
        text = res["content"][0]["text"]
        assert "older version" in text
        assert "update" in text
        assert "one at a time" in text

    async def test_old_bridge_directory_refusal_is_explained(self, users_dir, mgr):
        """A pre-T10 bridge also does not mark is_dir: it answers the stat
        probe with its old directory refusal. That is recognised as an old
        client too, not relayed as a program error."""
        _mk_user(users_dir, "alice")

        class OldBridge(FakeRegistry):
            async def call(self, user_id, method, params, timeout=None):
                if method == "transfer_upload" and params.get("stat_only"):
                    self.calls.append((method, params))
                    from localoctop.errors import CODE_INVALID_PARAMS
                    raise BridgeProtocolError(
                        CODE_INVALID_PARAMS, "path is a directory; send files one at a time")
                return await super().call(user_id, method, params, timeout=timeout)

        mgr.registry = OldBridge({"box/a.txt": (b"x", 1000)})
        svc = _tool_service(mgr)
        res = await svc._transfer_tool("alice", "pair-1", "fetch_to_cloud",
                                       {"path": "box"}, "box", _now(), "")
        assert res["isError"] is False
        text = res["content"][0]["text"]
        assert "older version" in text and "update" in text

    async def test_saved_aside_file_is_reported_in_the_batch_result(self, users_dir, mgr):
        """A folder name occupied by a DIRECTORY in the cloud: that one file
        lands under the existing rule (a free ".uploaded" name) and the batch
        result says so; the others land normally."""
        _mk_user(users_dir, "alice")
        landing = users_dir / "alice" / "localoctop" / "box"
        landing.mkdir(parents=True)
        (landing / "taken.txt").mkdir()  # a directory squats on the name
        reg = FakeRegistry(_files(("box/taken.txt", b"payload", 1000),
                                  ("box/free.txt", b"other", 1001)))
        mgr.registry = reg
        async with _client_for(mgr) as client:
            t = await mgr.start_upload_dir("alice", "pair-1", "box")
            await _drive_batch(mgr, client, t)
        assert t.state == "done", t.error
        assert (landing / "taken.txt").is_dir(), "the directory must be untouched"
        saved = [p for p in landing.iterdir() if p.name.startswith("taken.txt.uploaded")]
        assert len(saved) == 1 and saved[0].read_bytes() == b"payload"
        assert (landing / "free.txt").read_bytes() == b"other"
        text = mgr.describe(t)
        assert "saved under a different name" in text
        assert saved[0].name in text


def _tool_service(mgr) -> ToolService:
    class _Audit:
        async def record(self, **kw):
            pass
    svc = ToolService(_Settings(_store()), None, None)
    svc.transfers = mgr
    svc.audit = _Audit()
    return svc


def _now() -> float:
    return time.monotonic()


async def _wait_batch(mgr, t, timeout=15.0):
    deadline = time.monotonic() + timeout
    while t.state == "active" and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return t
