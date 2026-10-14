"""Tests for ToolService._render_read (LOX-002 T3b): a TEXT file larger than
10 MB is not paged. The bridge flags it `large_file`; the first screen is
rendered with a closing note that names fetch_to_cloud, and any later window
is a normal (isError=False) answer saying nothing was read — never a paging
hint, so the model cannot walk a huge file 100 lines at a time.

Rendering without the large_file flag must stay byte-identical to before.
"""

from __future__ import annotations

from localoctop.tools import ToolService


def _svc() -> ToolService:
    # _render_read touches no collaborators.
    return ToolService(None, None, None)


def _text(rendered: dict) -> str:
    blocks = rendered["content"]
    assert len(blocks) == 1 and blocks[0]["type"] == "text"
    return blocks[0]["text"]


class TestLargeFileFirstScreen:
    def test_no_paging_hint_and_points_at_fetch_to_cloud(self):
        result = {
            "path": "logs/huge.txt", "size": 15_000_000,
            "start_line": 1, "end_line": 100, "total_lines": 150_000,
            "eof": False, "text": "".join(f"line {i}\n" for i in range(1, 101)),
            "large_file": True,
        }
        rendered = _svc()._render_read(result)
        assert rendered["isError"] is False
        text = _text(rendered)
        # The first screen is still delivered.
        assert "lines 1-100" in text
        assert "line 1\n" in text
        # …with an explicit "this is a big file, only the beginning" note.
        assert "10 MB" in text
        assert "larger than 10 MB" in text
        # No paging instruction anywhere, and the way out is named with the path.
        assert "start_line=" not in text
        assert "next part" not in text
        assert "fetch_to_cloud" in text
        assert "logs/huge.txt" in text
        assert "path='logs/huge.txt'" in text

    def test_clipped_note_still_appended(self):
        result = {
            "path": "wide.txt", "size": 12_000_000,
            "start_line": 1, "end_line": 5, "total_lines": -1,
            "eof": False, "text": "a\n" * 5, "clipped_lines": 2, "large_file": True,
        }
        text = _text(_svc()._render_read(result))
        assert "2 very long line(s) were clipped" in text
        assert "start_line=" not in text


class TestLargeFileLaterPage:
    def test_nothing_read_is_a_normal_result_not_an_error(self):
        result = {
            "path": "logs/huge.txt", "size": 15_000_000,
            "start_line": 101, "end_line": 100, "total_lines": -1,
            "eof": False, "text": "", "large_file": True,
        }
        rendered = _svc()._render_read(result)
        # Not an error: a normal answer with what happened + what to do next.
        assert rendered["isError"] is False
        text = _text(rendered)
        assert "10 MB" in text
        assert "nothing was read" in text
        assert "start_line=" not in text
        assert "next part" not in text
        assert "fetch_to_cloud" in text
        assert "logs/huge.txt" in text
        assert "path='logs/huge.txt'" in text
        # The file size is reported so the model knows the scale.
        assert "15,000,000 bytes" in text
        # No body was rendered.
        assert "line 1\n" not in text


class TestNonLargeRenderingUnchanged:
    """Without the large_file flag the rendered text is exactly what it was
    before T3b (byte for byte), so existing behaviour is untouched."""

    def test_paged_window_keeps_the_paging_hint(self):
        result = {
            "path": "notes.txt", "size": 9999, "start_line": 1, "end_line": 100,
            "total_lines": 500, "eof": False, "text": "line 1\nline 2\n",
        }
        expected = (
            "== notes.txt (9,999 bytes): lines 1-100 of 500 ==\n"
            "line 1\nline 2\n"
            "[not the whole file — next part: read_file with start_line=101. "
            "To process the whole file, use fetch_to_cloud instead of paging.]"
        )
        rendered = _svc()._render_read(result)
        assert rendered["isError"] is False
        assert _text(rendered) == expected

    def test_eof_window(self):
        result = {
            "path": "small.txt", "size": 12, "start_line": 1, "end_line": 2,
            "total_lines": 2, "eof": True, "text": "a\nb\n",
        }
        expected = "== small.txt (12 bytes): lines 1-2 of 2 ==\na\nb\n[end of file]"
        assert _text(_svc()._render_read(result)) == expected

    def test_empty_window_past_end(self):
        result = {
            "path": "small.txt", "size": 12, "start_line": 10, "end_line": 9,
            "total_lines": 2, "eof": True, "text": "",
        }
        expected = "== small.txt (12 bytes): no lines at 10 of 2 ==\n[end of file]"
        assert _text(_svc()._render_read(result)) == expected

    def test_clipped_window(self):
        result = {
            "path": "wide.txt", "size": 5000, "start_line": 1, "end_line": 1,
            "total_lines": 1, "eof": True, "text": "x\n", "clipped_lines": 1,
        }
        expected = ("== wide.txt (5,000 bytes): lines 1-1 of 1 ==\nx\n"
                    "[end of file] [1 very long line(s) were clipped]")
        assert _text(_svc()._render_read(result)) == expected

    def test_unknown_total_lines_omits_the_count(self):
        result = {
            "path": "big.txt", "size": 20_000_000, "start_line": 1, "end_line": 100,
            "total_lines": -1, "eof": False, "text": "l\n",
        }
        expected = (
            "== big.txt (20,000,000 bytes): lines 1-100 ==\nl\n"
            "[not the whole file — next part: read_file with start_line=101. "
            "To process the whole file, use fetch_to_cloud instead of paging.]"
        )
        assert _text(_svc()._render_read(result)) == expected

    def test_binary_and_not_utf8_guidance_unchanged(self):
        binary = _svc()._render_read({
            "path": "img.png", "size": 2, "start_line": 1, "end_line": 0,
            "total_lines": -1, "eof": False, "text": "", "not_text": "binary",
        })
        assert binary["isError"] is False
        btext = _text(binary)
        assert "img.png" in btext and "binary" in btext
        assert "fetch_to_cloud" in btext and "'img.png'" in btext
        assert "Nothing was read." in btext

        not_utf8 = _svc()._render_read({
            "path": "notes-gbk.txt", "size": 1234, "start_line": 1, "end_line": 0,
            "total_lines": -1, "eof": False, "text": "", "not_text": "not_utf8",
        })
        assert not_utf8["isError"] is False
        ntext = _text(not_utf8)
        assert "not directly readable text" in ntext and "binary format" in ntext
        assert "a different character encoding" not in ntext
        assert "fetch_to_cloud" in ntext
