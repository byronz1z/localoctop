"""Tests for LOX-002 T5: the guidance the model is given.

T5-1  SERVER_INSTRUCTIONS covers the whole workflow in the right order and
      never contradicts the tool descriptions.
T5-2  Every error text reaching the model carries a concrete next step
      (which tool, which arguments) — applied on the SERVER so an old client
      program benefits too.
T5-3  Error texts never leak an absolute path of the employee's machine.
T5-4  A listing/search that is not complete says so, with the real total or
      a lower bound — the cap is never presented as the total.
"""

from __future__ import annotations

import asyncio

import pytest

from localoctop.errors import (
    BridgeProtocolError,
    CODE_INVALID_PARAMS,
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
)
from localoctop.mcp_app import SERVER_INSTRUCTIONS
from localoctop.protocol import TOOL_SCHEMAS, tools_list
from localoctop.tools import ToolService, _next_step_for_error

from conftest import make_session, FakeWebSocket


# ------------------------------------------------------------- T5-1

class TestServerInstructions:
    def test_covers_all_seven_points(self):
        s = SERVER_INSTRUCTIONS
        # 1. what the connector is for: allowed folders, originals stay.
        assert "allowed" in s and "originals stay" in s
        # 2. the look-around tools.
        for tool in ("list_allowed_directories", "list_directory",
                     "search_files", "get_file_info"):
            assert tool in s
        # 3. read_file bounds + the fetch_to_cloud line for everything else.
        assert "read_file" in s and "100 lines" in s and "48 KB" in s
        assert "10 MB" in s
        for kind in ("PDF", "Office", "images", "archives", "audio/video", "UTF-8"):
            assert kind in s
        assert "fetch_to_cloud" in s and "folder" in s
        # 4. deliver_to_local + the overwrite default.
        assert "deliver_to_local" in s and "overwrite=true" in s
        assert "explicitly asked" in s
        # 5. long transfers.
        assert "IN PROGRESS" in s and "transfer_status" in s
        assert "transfer_id" in s and "DONE" in s and "FAILED" in s
        # 6. recycle bin, no permanent delete.
        assert "recycle bin" in s and "no permanent delete" in s
        # 7. error handling: follow the next step, stop after two failures.
        assert "next step" in s and "fails twice" in s

    def test_length_within_budget(self):
        assert len(SERVER_INSTRUCTIONS) <= 2500

    def test_fetch_description_states_the_real_transfer_limit(self):
        """Batch-2: 'the per-transfer size limit' told the model nothing. The
        description must quote the same number TransferManager enforces."""
        from localoctop.protocol import MAX_TRANSFER_BYTES, TOOL_SCHEMAS
        from localoctop.transfers import DEFAULT_MAX_TRANSFER_BYTES
        fetch = TOOL_SCHEMAS["fetch_to_cloud"]["description"]
        assert f"{MAX_TRANSFER_BYTES:,} bytes" in fetch
        # Same source as the runtime default (no second hard-coded number).
        assert MAX_TRANSFER_BYTES == int(__import__("os").environ.get(
            "LOCALOCTOP_MAX_TRANSFER_BYTES", DEFAULT_MAX_TRANSFER_BYTES))

    def test_no_contradiction_with_tool_descriptions(self):
        descs = "\n".join(t["description"] for t in TOOL_SCHEMAS.values())
        # Nothing anywhere may offer permanent deletion.
        assert "permanent=true" not in descs
        assert "hard delete is available" not in descs
        # fetch_to_cloud takes folders (T10): the retired "ONE file only"
        # wording must not survive anywhere in the fetch description.
        fetch = TOOL_SCHEMAS["fetch_to_cloud"]["description"]
        assert "folder" in fetch.lower()
        assert "ONE file, or a FOLDER" in fetch
        deliver = TOOL_SCHEMAS["deliver_to_local"]["description"]
        assert "recycle bin" in deliver
        # Instructions themselves mention folders and the recycle bin.
        assert "folder" in SERVER_INSTRUCTIONS
        assert "recycle bin" in SERVER_INSTRUCTIONS
        # The catalog wires up: instructions name tools that actually exist.
        for tool in ("list_allowed_directories", "list_directory", "search_files",
                     "get_file_info", "read_file", "fetch_to_cloud",
                     "deliver_to_local", "transfer_status"):
            assert tool in TOOL_SCHEMAS


# ------------------------------------------------------------- T5-2

class TestNextStepForError:
    """Every message the current Go client can emit (plus the server's own
    refusals) maps to what-happened + next-step. Each case asserts the
    specific tool named in the guidance."""

    CASES = [
        # (client message, expected tool name in the appended guidance)
        ("file not found: t-B1/no-such-file-b1.txt", "list_directory"),
        ("path not found: docs/nope.txt", "list_directory"),
        ("directory not found: docs/nope", "search_files"),
        ("folder not found: docs", "list_directory"),
        ("search root not found", "search_files"),
        ("cloud file not found: /no-such-cloud-file-b1.txt", "cloud"),
        ("path is a directory; send files one at a time", "fetch_to_cloud"),
        ("path is a directory; use list_directory", "list_directory"),
        ("destination is a directory; give a file path", "list_directory"),
        ("path is not a directory", "get_file_info"),
        ('path traversal ("..") is not allowed', "list_allowed_directories"),
        ("path is outside the whitelist", "list_allowed_directories"),
        ("resolved path escapes the whitelist (symlink or traversal)", "list_allowed_directories"),
        ("relative path must not be absolute", "list_allowed_directories"),
        ("relative path must not contain a drive letter", "list_allowed_directories"),
        ("path contains NUL byte", "plain path"),
        ("destination already exists (move_file never overwrites)", "delete_file"),
        ("member target already exists: <allowed folder>/a.txt (pass overwrite=true)", "overwrite=true"),
        ("directory is not empty; pass recursive=true to remove its contents", "recursive=true"),
        ("not a valid zip archive: zip: not a valid zip file", "get_file_info"),
        ("archive member escapes destination: ../evil", "tell the user"),
        ("file is 26214400 bytes, exceeds read limit 20971520", "fetch_to_cloud"),
        ("edit_file requires a UTF-8 text file", "fetch_to_cloud"),
        ("write operations are disabled on this client", "write access"),
        ("the connected client does not allow write_file (write switch off on the machine)",
         "write access"),
        ("unknown method: transfer_manifest", "update"),
        ("refusing to remove a whitelist root directory", "inside the whitelist root"),
        ("source and destination are the same path", "different destination"),
        ("cannot move a directory into its own subtree", "outside the folder"),
        ("some totally unforeseen refusal", "do not retry blindly"),
    ]

    @pytest.mark.parametrize("message,needle", CASES)
    def test_every_error_carries_a_next_step(self, message, needle):
        out = _next_step_for_error(message)
        assert out.startswith(message)
        assert "Next step" in out or "tell the user" in out
        assert needle in out

    def test_move_file_conflict_never_offers_overwrite(self):
        """G1 batch-2: move_file has NO overwrite parameter — the old hint sent
        the model looking for one and the retry was refused again. The hint
        must only name steps that actually work."""
        msg = "destination already exists (move_file never overwrites)"
        out = _next_step_for_error(msg)
        assert out.startswith(msg)
        appended = out[len(msg):]
        # The appended guidance must not offer a parameter the tool lacks.
        assert "overwrite=true" not in appended
        assert "different destination name" in appended
        assert "delete_file" in appended and "recycle bin" in appended

    def test_unzip_conflict_keeps_overwrite_hint(self):
        """unzip_file DOES take overwrite=true; its 'already exists' wording
        must keep pointing at it (the shared branch must not regress it)."""
        out = _next_step_for_error("member target already exists: x/a.txt (pass overwrite=true)")
        assert "overwrite=true" in out
        assert "delete_file" not in out

    def test_message_with_guidance_is_left_alone(self):
        msg = ("the recycle bin is not available on this computer ... untouched. "
               "Next step: write to a new file name instead, or tell the user.")
        assert _next_step_for_error(msg) == msg

    def test_transfer_status_text_left_alone(self):
        msg = ("no transfer 'abc' (finished long ago, or the server restarted) — "
               "start it again with fetch_to_cloud / deliver_to_local")
        # Carries its own way forward ("start it again ..."); the enricher
        # must not stack a generic hint on top.
        assert _next_step_for_error(msg) == msg


class TestEnrichedErrorsEndToEnd:
    """T5-2 through the real ToolService: a fake bridge answering with the
    OLD bare wording must still yield a next step (new server + old client)."""

    def _svc(self, registry, settings):
        svc = ToolService(settings, registry, None)

        async def _noop_audit(**kw):
            return None
        svc.audit = type("A", (), {"record": staticmethod(_noop_audit)})()
        return svc

    @pytest.mark.asyncio
    async def test_old_client_not_found_gets_next_step(self, settings, registry):
        ws = FakeWebSocket()
        session = make_session("alice", ws, write_enabled=False)
        await registry.register(session)
        svc = self._svc(registry, settings)

        async def fail_call(*a, **kw):
            raise BridgeProtocolError(CODE_NOT_FOUND, "file not found: t-B1/no-such-file-b1.txt")
        registry.call = fail_call  # type: ignore[assignment]

        res = await svc.call_tool("alice", "", "read_file", {"path": "t-B1/no-such-file-b1.txt"})
        assert res["isError"] is True
        text = res["content"][0]["text"]
        assert "file not found" in text
        assert "list_directory" in text and "search_files" in text

    @pytest.mark.asyncio
    async def test_old_client_whitelist_denial_gets_next_step(self, settings, registry):
        ws = FakeWebSocket()
        session = make_session("alice", ws, write_enabled=False)
        await registry.register(session)
        svc = self._svc(registry, settings)

        async def fail_call(*a, **kw):
            raise BridgeProtocolError(CODE_NOT_ALLOWED, "path is outside the whitelist")
        registry.call = fail_call  # type: ignore[assignment]

        res = await svc.call_tool("alice", "", "read_file", {"path": "/etc/passwd"})
        text = res["content"][0]["text"]
        assert "list_allowed_directories" in text

    @pytest.mark.asyncio
    async def test_old_client_directory_refusal_gets_next_step(self, settings, registry):
        ws = FakeWebSocket()
        session = make_session("alice", ws, write_enabled=False)
        await registry.register(session)
        svc = self._svc(registry, settings)

        async def fail_call(*a, **kw):
            raise BridgeProtocolError(CODE_INVALID_PARAMS, "path is a directory; use list_directory")
        registry.call = fail_call  # type: ignore[assignment]

        res = await svc.call_tool("alice", "", "read_file", {"path": "docs"})
        text = res["content"][0]["text"]
        assert "list_directory" in text or "fetch_to_cloud" in text


# ------------------------------------------------------------- T5-3

class TestNoMachinePathsInErrors:
    def test_allowed_root_is_scrubbed(self):
        from localoctop.tools import _strip_machine_paths
        msg = "member target already exists: D:/ZBS-Project/work/a.txt (pass overwrite=true)"
        out = _strip_machine_paths(msg, ["D:/ZBS-Project/work"])
        assert "D:" not in out and "ZBS-Project" not in out
        assert "<allowed folder>" in out

    def test_backslash_variant_scrubbed(self):
        from localoctop.tools import _strip_machine_paths
        msg = r"path is outside the whitelist: C:\Users\byron\docs\x.txt"
        out = _strip_machine_paths(msg, ["C:\\Users\\byron\\docs"])
        assert "C:" not in out

    @pytest.mark.asyncio
    async def test_end_to_end_error_carries_no_absolute_path(self, settings, registry):
        """The registered session's allowed dir must never appear in the
        error text the model receives."""
        ws = FakeWebSocket()
        root = "/home/test/Documents/Work"  # make_session's allowed dir
        session = make_session("alice", ws, write_enabled=False)
        await registry.register(session)
        svc = ToolService(settings, registry, None)

        async def _noop_audit(**kw):
            return None
        svc.audit = type("A", (), {"record": staticmethod(_noop_audit)})()

        async def fail_call(*a, **kw):
            raise BridgeProtocolError(
                CODE_INVALID_PARAMS,
                f"member target already exists: {root}/a.txt (pass overwrite=true)")
        registry.call = fail_call  # type: ignore[assignment]

        # read_file is read-only, so no write gate stands between the call
        # and the (failing) bridge; the scrubbing under test is identical.
        res = await svc.call_tool("alice", "", "read_file", {"path": "a.txt"})
        text = res["content"][0]["text"]
        assert root not in text
        assert "/home/test" not in text


# ------------------------------------------------------------- T5-4

class TestListingHonesty:
    def _svc(self) -> ToolService:
        return ToolService(None, None, None)

    def _text(self, rendered) -> str:
        return "\n".join(b["text"] for b in rendered["content"] if b["type"] == "text")

    def test_truncated_listing_states_lower_bound_and_next_step(self):
        res = {"path": ".", "entries": [{"name": f"f{i}.txt", "type": "file",
                                         "size": 1, "mtime": ""} for i in range(500)],
               "truncated": True, "total_known": False, "total_entries": 6000, "limit": 500}
        out = self._svc()._render("list_directory", res)
        text = self._text(out)
        assert "NOT the whole listing" in text
        assert "at least 6,000" in text
        assert "search_files" in text  # the way forward

    def test_truncated_listing_with_exact_total(self):
        res = {"path": ".", "entries": [{"name": "a", "type": "file", "size": 1, "mtime": ""}],
               "truncated": True, "total_known": True, "total_entries": 3, "limit": 1}
        text = self._text(self._svc()._render("list_directory", res))
        assert "3 entries in total" in text

    def test_complete_listing_has_no_warning(self):
        res = {"path": ".", "entries": [{"name": "a", "type": "file", "size": 1, "mtime": ""}],
               "truncated": False, "total_known": True, "total_entries": 1, "limit": 500}
        text = self._text(self._svc()._render("list_directory", res))
        assert "NOT the whole" not in text and "may NOT" not in text

    def test_old_client_at_cap_says_maybe_incomplete(self):
        """New server + OLD client (no truncation fields): when the count
        equals a round cap the listing must not read as complete."""
        res = {"path": ".", "entries": [{"name": f"f{i}", "type": "file",
                                         "size": 1, "mtime": ""} for i in range(500)]}
        text = self._text(self._svc()._render("list_directory", res))
        assert "may NOT be the whole listing" in text

    def test_old_client_below_cap_is_quiet(self):
        res = {"path": ".", "entries": [{"name": "a", "type": "file", "size": 1, "mtime": ""}]}
        text = self._text(self._svc()._render("list_directory", res))
        assert "may NOT" not in text

    def test_with_sizes_cap_is_not_the_total(self):
        """The 6000-file folder must never read 'totals: 5000 files'."""
        res = {"path": ".",
               "entries": [{"name": f"f{i}", "type": "file", "size": 1} for i in range(5000)],
               "total_files": 5000, "total_dirs": 0, "total_bytes": 5000,
               "truncated": True, "totals_count_all": False, "total_entries_seen": 6000}
        text = self._text(self._svc()._render("list_directory_with_sizes", res))
        assert "NOT the whole folder" in text
        assert "more than 6000" in text
        assert "totals of the 5000 listed entries" in text
        # The bare "totals: N files" claim (the defect) is gone.
        assert "\ntotals: 5000 files" not in text

    def test_with_sizes_full_walk_keeps_true_totals(self):
        res = {"path": ".", "entries": [{"name": "a", "type": "file", "size": 3}],
               "total_files": 1, "total_dirs": 0, "total_bytes": 3,
               "truncated": False, "totals_count_all": True, "total_entries_seen": 1}
        text = self._text(self._svc()._render("list_directory_with_sizes", res))
        assert "totals: 1 files" in text
        assert "NOT the whole folder" not in text

    def test_search_skipped_large_files_named(self):
        res = {"pattern": "needle", "path": ".", "matches": [],
               "skipped_large": 2, "skipped_large_names": ["big1.log", "big2.log"]}
        text = self._text(self._svc()._render("search_files", res))
        assert "2 file(s) were NOT searched" in text
        assert "big1.log" in text and "big2.log" in text
        assert "fetch_to_cloud" in text
        # "0 matches" must not read as "searched everywhere".
        assert "NOT ruled out" in text

    def test_search_no_skip_no_note(self):
        res = {"pattern": "x", "path": ".", "matches": [], "skipped_large": 0}
        text = self._text(self._svc()._render("search_files", res))
        assert "NOT searched" not in text and "NOT ruled out" not in text
        assert "(nothing found)" in text

    def test_search_skip_note_names_the_size_line(self):
        """Batch-2: the skip note must state the actual size line the client
        skipped at (its MaxReadBytes), so the model never guesses it (G2 saw
        a model assume 10 MiB when the real line is 20 MiB)."""
        res = {"pattern": "needle", "path": ".", "matches": [], "skipped_large": 1,
               "skipped_large_names": ["big.log"], "skipped_large_limit": 20 * 1024 * 1024}
        text = self._text(self._svc()._render("search_files", res))
        assert "20 MiB" in text
        assert f"{20 * 1024 * 1024:,} bytes" in text
        assert "big.log" in text


# ------------------------------------------------------------- T5-5b render

class TestUnzipRender:
    def test_recycled_members_reported(self):
        svc = ToolService(None, None, None)
        out = svc._render("unzip_file", {
            "archive": "a.zip", "dest": ".", "extracted": 3, "total_bytes": 14,
            "recycled": ["a.txt", "sub/b.txt"]})
        text = "\n".join(b["text"] for b in out["content"])
        assert "recycle bin" in text
        assert "a.txt" in text and "sub/b.txt" in text
        assert "2 existing file(s)" in text

    def test_old_client_no_recycled_field_stays_quiet(self):
        svc = ToolService(None, None, None)
        out = svc._render("unzip_file", {"archive": "a.zip", "dest": ".",
                                         "extracted": 3, "total_bytes": 14})
        text = "\n".join(b["text"] for b in out["content"])
        assert "extracted 3" in text
        assert "recycle bin" not in text  # nothing is claimed either way


# ------------------------------------------- T5-5 / T5-6 / T6-1: describe()

class _StubSettings:
    bridge_timeout = 2.0

    def __init__(self):
        self.token_store = None


def _mgr_for_describe(tmp_path, monkeypatch):
    """A TransferManager good enough for describe(): no registry calls, just
    the text rendering of a finished record."""
    from localoctop.transfers import TransferManager
    monkeypatch.setenv("LOCALOCTOP_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("LOCALOCTOP_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "users").mkdir()
    return TransferManager(_StubSettings(), None, "/mcp/localoctop/transfer")


def _done_down(mgr, **result):
    from pathlib import Path
    t = mgr._new(user_id="alice", pair_id="pair-1", direction="down",
                 local_path="out.txt", cloud_path=Path("/out.txt"), size=10)
    t.result = result
    t.finish(True)
    return t


class TestDescribeDelivery:
    def test_replaced_old_client_wording(self, tmp_path, monkeypatch):
        """An old client reports only replaced=true: the bin is its
        documented first choice, so the text says so."""
        mgr = _mgr_for_describe(tmp_path, monkeypatch)
        t = _done_down(mgr, path="out.txt", replaced=True)
        text = mgr.describe(t)
        assert "moved to the recycle bin" in text

    def test_replaced_via_rename_is_reported_truthfully(self, tmp_path, monkeypatch):
        """T5-5: when the bin was unavailable and the old version was
        renamed aside, the text must say THAT — not claim the bin."""
        mgr = _mgr_for_describe(tmp_path, monkeypatch)
        t = _done_down(mgr, path="out.txt", replaced=True,
                       set_aside="renamed:out.txt.bak-20261014-010203")
        text = mgr.describe(t)
        assert "renamed to out.txt.bak-20261014-010203" in text
        # The default bin wording ("The previous version was moved to the
        # recycle bin.") must not appear — that would be the T5-5 lie.
        assert "The previous version was moved to the recycle bin." not in text

    def test_already_present_is_a_noop_report(self, tmp_path, monkeypatch):
        """T6-1: byte-identical file already on the machine — nothing was
        written and no 'name (1).ext' duplicate was made."""
        mgr = _mgr_for_describe(tmp_path, monkeypatch)
        t = _done_down(mgr, path="out.txt", replaced=False, already_present=True)
        text = mgr.describe(t)
        assert "already on the user's machine" in text
        assert "no second copy" in text
        assert "recycle bin" not in text


class TestDescribeKeptCopy:
    def _done_up(self, mgr, **result):
        from pathlib import Path
        base = {"sha256": "ab", "visible": "/localoctop/doc.txt"}
        base.update(result)
        t = mgr._new(user_id="alice", pair_id="pair-1", direction="up",
                     local_path="doc.txt", cloud_path=Path("/localoctop/doc.txt"), size=4)
        t.result = base
        t.finish(True)
        return t

    def test_modified_copy_wording_unchanged(self, tmp_path, monkeypatch):
        """A PROVEN modification keeps the existing wording."""
        mgr = _mgr_for_describe(tmp_path, monkeypatch)
        t = self._done_up(mgr, kept_as="/localoctop/doc.cloud-edited-20260101-000000.txt",
                          kept_as_list=["/localoctop/doc.cloud-edited-20260101-000000.txt"])
        text = mgr.describe(t)
        assert "had been modified and was kept as" in text

    def test_unconfirmable_copy_is_described_as_unconfirmable(self, tmp_path, monkeypatch):
        """T5-6: no ledger record (e.g. right after an upgrade) — the text
        must NOT claim the copy had been modified."""
        mgr = _mgr_for_describe(tmp_path, monkeypatch)
        name = "/localoctop/doc.cloud-edited-20260101-000000.txt"
        t = self._done_up(mgr, kept_as=name, kept_as_list=[name], kept_unknown=[name])
        text = mgr.describe(t)
        assert "could not be confirmed" in text
        assert "kept as" in text
        assert "had been modified" not in text


class TestTransferStatusUnknownId:
    """T6-3: the server-restart wording already carries its next step; this
    pins it so it is not lost."""
    def test_unknown_transfer_names_the_restart_and_the_next_step(self, tmp_path, monkeypatch):
        mgr = _mgr_for_describe(tmp_path, monkeypatch)
        with pytest.raises(BridgeProtocolError) as ei:
            mgr.get_for_user("alice", "gone")
        msg = ei.value.message
        assert "server restarted" in msg
        assert "fetch_to_cloud" in msg and "deliver_to_local" in msg
