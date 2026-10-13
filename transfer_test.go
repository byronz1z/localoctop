package localoctop

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
)

// ---- readText: bounded, line-based text reading ----

func TestReadTextLineWindow(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	body := "l1\nl2\nl3\nl4\nl5\n"
	must(t, os.WriteFile(filepath.Join(root, "win.txt"), []byte(body), 0o644))

	res, berr := tl.readText(map[string]any{"path": "win.txt", "start_line": 2, "max_lines": 2})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if res.StartLine != 2 || res.EndLine != 3 || res.Text != "l2\nl3\n" {
		t.Fatalf("bad window: %+v text=%q", res, res.Text)
	}
	if res.EOF {
		t.Fatal("window of a 5-line file at lines 2-3 must not report EOF")
	}
	if res.TotalLines != 5 {
		t.Fatalf("small file should be fully counted, got %d", res.TotalLines)
	}

	// Asking past the end yields an empty window at EOF.
	res, berr = tl.readText(map[string]any{"path": "win.txt", "start_line": 10})
	if berr != nil {
		t.Fatalf("readText past EOF failed: %v", berr)
	}
	if !res.EOF || res.Text != "" || res.TotalLines != 5 {
		t.Fatalf("past-EOF window wrong: %+v", res)
	}
	if res.EndLine != 9 { // start + taken - 1 with taken == 0
		t.Fatalf("empty window end_line should be start-1, got %d", res.EndLine)
	}
}

func TestReadTextDefaultsAndCaps(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.WriteFile(filepath.Join(root, "one.txt"), []byte("only\n"), 0o644))

	// Out-of-range start_line clamps to 1; bogus max_lines falls back to the
	// default window.
	res, berr := tl.readText(map[string]any{"path": "one.txt", "start_line": -5, "max_lines": 0})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if res.StartLine != 1 || res.Text != "only\n" || !res.EOF {
		t.Fatalf("clamp/default wrong: %+v", res)
	}
}

func TestReadTextUTF8NotSplit(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	// Each line is long but made of multi-byte runes; readText must never cut
	// a character in half, even when the byte budget forces a stop.
	var sb strings.Builder
	for i := 0; i < 500; i++ {
		sb.WriteString(strings.Repeat("中", 100) + "\n")
	}
	must(t, os.WriteFile(filepath.Join(root, "cjk.txt"), []byte(sb.String()), 0o644))

	res, berr := tl.readText(map[string]any{"path": "cjk.txt"})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if !utf8.ValidString(res.Text) {
		t.Fatal("returned text is not valid UTF-8 — a multi-byte character was split")
	}
	if len(res.Text) > maxTextBytes {
		t.Fatalf("text budget exceeded: %d > %d", len(res.Text), maxTextBytes)
	}
	if res.EOF {
		t.Fatal("a 150 KB file must not fit into one 48 KB window")
	}
	if res.TotalLines != 500 {
		t.Fatalf("small file should be fully counted, got %d", res.TotalLines)
	}
}

func TestReadTextBinaryRefused(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	bin := append([]byte("MZ"), make([]byte, 100)...) // NUL bytes early on
	must(t, os.WriteFile(filepath.Join(root, "app.exe"), bin, 0o644))

	_, berr := tl.readText(map[string]any{"path": "app.exe"})
	if berr == nil {
		t.Fatal("binary file must be refused")
	}
	if berr.Code != CodeInvalidParams {
		t.Fatalf("expected CodeInvalidParams, got %d", berr.Code)
	}
	if !strings.Contains(berr.Message, "binary") || !strings.Contains(berr.Message, "fetch_to_cloud") {
		t.Fatalf("refusal must name the file type and the way out: %q", berr.Message)
	}
}

func TestReadTextLongLineClipped(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	long := strings.Repeat("x", maxTextLineBytes+500)
	must(t, os.WriteFile(filepath.Join(root, "long.txt"), []byte(long+"\nnext\n"), 0o644))

	res, berr := tl.readText(map[string]any{"path": "long.txt"})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if res.Clipped != 1 {
		t.Fatalf("expected 1 clipped line, got %d", res.Clipped)
	}
	if !strings.Contains(res.Text, "[line clipped]") {
		t.Fatalf("clipped line must be marked: %q", res.Text[:80])
	}
	if !strings.Contains(res.Text, "next\n") {
		t.Fatal("the line after the clipped one must still be delivered")
	}
	// The clipped payload itself stays within the per-line cap.
	if len(strings.Split(res.Text, "\n")[0]) > maxTextLineBytes+40 {
		t.Fatalf("clipped line exceeds cap: %d", len(strings.Split(res.Text, "\n")[0]))
	}
}

func TestReadTextErrors(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.MkdirAll(filepath.Join(root, "dir"), 0o755))

	if _, berr := tl.readText(map[string]any{"path": "missing.txt"}); berr == nil || berr.Code != CodeNotFound {
		t.Fatalf("missing file must be CodeNotFound, got %v", berr)
	}
	if _, berr := tl.readText(map[string]any{"path": "dir"}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("directory must be CodeInvalidParams, got %v", berr)
	}
}

// ---- freeName ----

func TestFreeName(t *testing.T) {
	root := t.TempDir()
	p := filepath.Join(root, "report.pdf")

	// The name is taken -> the first numbered sibling comes back.
	must(t, os.WriteFile(p, []byte("v0"), 0o644))
	got := freeName(p)
	if want := filepath.Join(root, "report (1).pdf"); got != want {
		t.Fatalf("want %q, got %q", want, got)
	}
	must(t, os.WriteFile(got, []byte("v1"), 0o644))
	if want := filepath.Join(root, "report (2).pdf"); freeName(p) != want {
		t.Fatalf("want %q, got %q", want, freeName(p))
	}

	// Extension-less and dot-file names keep working.
	q := filepath.Join(root, "Makefile")
	must(t, os.WriteFile(q, []byte("x"), 0o644))
	if want := filepath.Join(root, "Makefile (1)"); freeName(q) != want {
		t.Fatalf("want %q, got %q", want, freeName(q))
	}
}

func TestFreeNameFallbackAfter999(t *testing.T) {
	root := t.TempDir()
	p := filepath.Join(root, "f.txt")
	must(t, os.WriteFile(p, []byte("x"), 0o644))
	for i := 1; i < 1000; i++ {
		must(t, os.WriteFile(filepath.Join(root, sprintf("f (%d).txt", i)), []byte("x"), 0o644))
	}
	got := freeName(p)
	// The numbered space is exhausted: a timestamped sibling must be used,
	// and it must not collide with any of the 1000 existing names.
	if strings.Contains(got, "(") {
		t.Fatalf("expected timestamped fallback, got %q", got)
	}
	if _, err := os.Stat(got); !os.IsNotExist(err) {
		t.Fatalf("fallback name must be free: %q", got)
	}
	if !strings.HasPrefix(got, filepath.Join(root, "f ")) || !strings.HasSuffix(got, ".txt") {
		t.Fatalf("fallback keeps stem and extension: %q", got)
	}
}
