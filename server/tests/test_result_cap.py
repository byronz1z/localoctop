"""Tests for ToolService._cap (LOX-002 round 3): an over-limit tool result
is a normal answer that keeps what fits — cut on character and line
boundaries — with a closing note on how to narrow the request. Embedded
binary blocks likewise render as a normal pointer at fetch_to_cloud.
"""

from __future__ import annotations

import localoctop.tools as tools_mod
from localoctop.tools import ToolService


def _svc() -> ToolService:
    # _cap touches no collaborators.
    return ToolService(None, None, None)


def _big_text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


class TestCapTruncation:
    def test_over_cap_text_is_kept_head_not_error(self, monkeypatch):
        monkeypatch.setattr(tools_mod, "MODEL_RESULT_CAP_BYTES", 4096)
        # Chinese content: cutting must never split a multi-byte character.
        line = "中文内容行" * 20 + "\n"  # 101 bytes per line
        text = line * 200
        rendered = _svc()._cap("list_directory", _big_text_result(text))
        assert rendered["isError"] is False
        out = "".join(b["text"] for b in rendered["content"] if b["type"] == "text")
        data = out.encode("utf-8")
        assert len(data) <= 4096
        # The kept head is valid UTF-8 (no half character) — .encode above
        # would have raised on a lone surrogate, and the head still decodes.
        head = rendered["content"][0]["text"]
        head.encode("utf-8").decode("utf-8")
        # Cut on a line boundary: the kept head followed by one newline is a
        # prefix of the original text (only whole lines were kept).
        assert head == "" or text.startswith(head + "\n")
        assert "result too large" in out
        # The note says how big the full answer was and what to do instead.
        assert f"{len(text.encode('utf-8')):,}" in out
        assert "Narrow the request" in out

    def test_under_cap_untouched(self, monkeypatch):
        monkeypatch.setattr(tools_mod, "MODEL_RESULT_CAP_BYTES", 4096)
        rendered = _svc()._cap("read_file", _big_text_result("small answer"))
        assert rendered == {"content": [{"type": "text", "text": "small answer"}], "isError": False}

    def test_binary_block_is_normal_pointer(self):
        rendered = {
            "content": [{"type": "resource", "resource": {"uri": "file:///x", "blob": "AA=="}}],
            "isError": False,
        }
        out = _svc()._cap("read_media_file", rendered)
        assert out["isError"] is False
        text = out["content"][0]["text"]
        assert "binary" in text and "fetch_to_cloud" in text


class TestCapNotesPerTool:
    """Round 6, item 10: the truncation note is per tool category, must not
    claim things that did not happen, and must not call the shown part the
    complete result."""

    def _note(self, monkeypatch, tool: str) -> str:
        monkeypatch.setattr(tools_mod, "MODEL_RESULT_CAP_BYTES", 4096)
        text = ("line of text\n" * 500)
        out = _svc()._cap(tool, _big_text_result(text))
        assert out["isError"] is False
        return out["content"][-1]["text"]

    def test_listing_note_suggests_narrowing_scope(self, monkeypatch):
        note = self._note(monkeypatch, "list_directory")
        assert "Narrow the request" in note
        assert "sub-directory" in note or "excludePatterns" in note

    def test_edit_note_asserts_neither_success_nor_failure(self, monkeypatch):
        note = self._note(monkeypatch, "edit_file")
        assert "only the first part" in note
        # No claim about the edit's outcome, and the shown part is not
        # called the whole result.
        for word in ("applied", "failed to match", "the whole result",
                     "succeeded", "was attempted"):
            assert word not in note

    def test_other_tools_get_generic_note_without_edit_wording(self, monkeypatch):
        note = self._note(monkeypatch, "get_file_info")
        assert "only the first part" in note
        assert "narrow the request" in note
        assert "edit" not in note.lower()
        assert "the whole result" not in note
