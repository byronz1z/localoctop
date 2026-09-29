package localoctop

import (
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// newTestTools wires a tools instance rooted at a temp dir with a tiny read
// limit so size-cap tests stay fast.
func newTestTools(t *testing.T, allowWrite bool, maxRead int64) (*tools, string) {
	t.Helper()
	root := t.TempDir()
	must(t, os.WriteFile(filepath.Join(root, "hello.txt"), []byte("hi there\n"), 0o644))
	must(t, os.MkdirAll(filepath.Join(root, "docs"), 0o755))
	must(t, os.WriteFile(filepath.Join(root, "docs", "a.md"), []byte("# A\napple\n"), 0o644))
	must(t, os.WriteFile(filepath.Join(root, "docs", "b.md"), []byte("# B\nbanana\n"), 0o644))

	cfg := NewConfig()
	// The tool handlers never dial, but Validate() requires the connection
	// fields (ServerURL/Token are mandatory in production), so supply valid
	// dummy values — same pattern as TestHandleRequest_Direct.
	cfg.ServerURL = "wss://adapter.example.com/mcp/localoctop/ws"
	cfg.Token = "test-token"
	cfg.AllowedDirs = []string{root}
	cfg.AllowWrite = allowWrite
	if maxRead > 0 {
		cfg.MaxReadBytes = maxRead
	}
	cfg.Logger = NopLogger{}
	must(t, cfg.Validate())

	guard := newPathGuard(cfg.AllowedDirs, NopLogger{})
	return newTools(cfg, guard, NopLogger{}), root
}

func TestListDirectory(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.listDirectory(map[string]any{"path": "."})
	if berr != nil {
		t.Fatalf("list_directory failed: %v", berr)
	}
	if len(res.Entries) < 2 {
		t.Fatalf("expected at least 2 entries, got %d", len(res.Entries))
	}
	var sawFile, sawDir bool
	for _, e := range res.Entries {
		if e.Name == "hello.txt" && e.Type == "file" {
			sawFile = true
		}
		if e.Name == "docs" && e.Type == "dir" {
			sawDir = true
		}
	}
	if !sawFile || !sawDir {
		t.Fatalf("missing expected entries: file=%v dir=%v", sawFile, sawDir)
	}
}

func TestListDirectory_NotADirectory(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	if _, berr := tl.listDirectory(map[string]any{"path": "hello.txt"}); berr == nil {
		t.Fatal("expected error listing a file as a directory")
	} else if berr.Code != CodeInvalidParams {
		t.Fatalf("expected CodeInvalidParams, got %d", berr.Code)
	}
}

func TestReadFile_Text(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.readFile(map[string]any{"path": "hello.txt"})
	if berr != nil {
		t.Fatalf("read_file failed: %v", berr)
	}
	if res.Encoding != "utf-8" || res.Text != "hi there\n" {
		t.Fatalf("unexpected content: encoding=%s text=%q", res.Encoding, res.Text)
	}
}

func TestReadFile_Binary(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.WriteFile(filepath.Join(root, "blob.bin"), []byte{0x00, 0x01, 0xff, 0xfe}, 0o644))
	res, berr := tl.readFile(map[string]any{"path": "blob.bin"})
	if berr != nil {
		t.Fatalf("read_file failed: %v", berr)
	}
	if res.Encoding != "base64" {
		t.Fatalf("expected base64 encoding for binary, got %s", res.Encoding)
	}
	dec, err := base64.StdEncoding.DecodeString(res.Base64)
	if err != nil || len(dec) != 4 || dec[2] != 0xff {
		t.Fatalf("bad base64 round-trip: %v %v", dec, err)
	}
}

// TestReadFile_SizeCap proves the 20MB (here: tiny) read limit is enforced.
func TestReadFile_SizeCap(t *testing.T) {
	tl, root := newTestTools(t, false, 16) // 16-byte cap
	must(t, os.WriteFile(filepath.Join(root, "big.txt"), []byte(strings.Repeat("x", 100)), 0o644))
	_, berr := tl.readFile(map[string]any{"path": "big.txt"})
	if berr == nil {
		t.Fatal("expected oversized read to be rejected")
	}
	if berr.Code != CodeTooLarge {
		t.Fatalf("expected CodeTooLarge, got %d", berr.Code)
	}
}

func TestReadFile_Missing(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	_, berr := tl.readFile(map[string]any{"path": "nope.txt"})
	if berr == nil || berr.Code != CodeNotFound {
		t.Fatalf("expected CodeNotFound, got %v", berr)
	}
}

func TestReadFile_RejectsTraversal(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	_, berr := tl.readFile(map[string]any{"path": "../../../etc/passwd"})
	if berr == nil || berr.Code != CodeNotAllowed {
		t.Fatalf("expected traversal read to be denied with CodeNotAllowed, got %v", berr)
	}
}

func TestSearchFiles_Glob(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.searchFiles(map[string]any{"path": "docs", "pattern": "*.md"})
	if berr != nil {
		t.Fatalf("search_files failed: %v", berr)
	}
	if len(res.Matches) != 2 {
		t.Fatalf("expected 2 md matches, got %d (%+v)", len(res.Matches), res.Matches)
	}
}

func TestSearchFiles_Substring(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.searchFiles(map[string]any{"path": ".", "pattern": "a.md"})
	if berr != nil {
		t.Fatalf("search_files failed: %v", berr)
	}
	if len(res.Matches) < 1 {
		t.Fatalf("expected at least 1 match for 'a.md'")
	}
}

func TestSearchFiles_RequiresPattern(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	if _, berr := tl.searchFiles(map[string]any{"path": "."}); berr == nil {
		t.Fatal("expected missing pattern to be rejected")
	}
}

func TestGetFileInfo(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	fi, berr := tl.getFileInfo(map[string]any{"path": "hello.txt"})
	if berr != nil {
		t.Fatalf("get_file_info failed: %v", berr)
	}
	if fi.Type != "file" || fi.Name != "hello.txt" || fi.Size != int64(len("hi there\n")) {
		t.Fatalf("unexpected file info: %+v", fi)
	}
	if fi.MIMEGuess != "text/plain" {
		t.Fatalf("unexpected mime %s", fi.MIMEGuess)
	}
}

func TestGetFileInfo_Dir(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	fi, berr := tl.getFileInfo(map[string]any{"path": "docs"})
	if berr != nil {
		t.Fatalf("get_file_info failed: %v", berr)
	}
	if fi.Type != "dir" {
		t.Fatalf("expected dir type, got %s", fi.Type)
	}
}

// TestWriteDisabledByDefault proves the reserved write tools are off unless
// AllowWrite is set — the "配置开关默认关" requirement.
func TestWriteDisabledByDefault(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	if _, berr := tl.writeFile(map[string]any{"path": "out.txt", "content": "x"}); berr == nil {
		t.Fatal("expected write_file to be rejected while AllowWrite=false")
	} else if berr.Code != CodeWriteDisabled {
		t.Fatalf("expected CodeWriteDisabled, got %d", berr.Code)
	}
	if _, berr := tl.createDirectory(map[string]any{"path": "newdir"}); berr == nil {
		t.Fatal("expected create_directory to be rejected while AllowWrite=false")
	} else if berr.Code != CodeWriteDisabled {
		t.Fatalf("expected CodeWriteDisabled, got %d", berr.Code)
	}
}

func TestWriteEnabled(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	res, berr := tl.writeFile(map[string]any{"path": "out.txt", "content": "written"})
	if berr != nil {
		t.Fatalf("write_file failed: %v", berr)
	}
	if !res.Created || res.Size != int64(len("written")) {
		t.Fatalf("unexpected write result: %+v", res)
	}
	data, err := os.ReadFile(filepath.Join(root, "out.txt"))
	if err != nil || string(data) != "written" {
		t.Fatalf("file not written correctly: %q %v", data, err)
	}
}

// TestWriteEnabled_StillRejectsTraversal ensures turning on writes does NOT
// loosen the path guard.
func TestWriteEnabled_StillRejectsTraversal(t *testing.T) {
	tl, _ := newTestTools(t, true, 0)
	if _, berr := tl.writeFile(map[string]any{"path": "../../evil.txt", "content": "x"}); berr == nil {
		t.Fatal("expected traversal write to be rejected even with AllowWrite=true")
	} else if berr.Code != CodeNotAllowed {
		t.Fatalf("expected CodeNotAllowed, got %d", berr.Code)
	}
}

func TestWriteEnabled_SizeCap(t *testing.T) {
	tl, _ := newTestTools(t, true, 0)
	tl.cfg.MaxWriteBytes = 8
	big := strings.Repeat("y", 100)
	if _, berr := tl.writeFile(map[string]any{"path": "big.txt", "content": big}); berr == nil {
		t.Fatal("expected oversized write to be rejected")
	} else if berr.Code != CodeTooLarge {
		t.Fatalf("expected CodeTooLarge, got %d", berr.Code)
	}
}

func TestCreateDirectory(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	res, berr := tl.createDirectory(map[string]any{"path": "made/inner"})
	if berr != nil {
		t.Fatalf("create_directory failed: %v", berr)
	}
	if !res.Created {
		t.Fatal("expected created=true")
	}
	st, err := os.Stat(filepath.Join(root, "made", "inner"))
	if err != nil || !st.IsDir() {
		t.Fatalf("directory not created: %v", err)
	}
}

func TestWriteFile_Base64(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	payload := base64.StdEncoding.EncodeToString([]byte{0x00, 0xff, 0x10})
	_, berr := tl.writeFile(map[string]any{"path": "b.bin", "content": payload, "encoding": "base64"})
	if berr != nil {
		t.Fatalf("base64 write failed: %v", berr)
	}
	data, err := os.ReadFile(filepath.Join(root, "b.bin"))
	if err != nil || len(data) != 3 || data[1] != 0xff {
		t.Fatalf("bad base64 write: %v %v", data, err)
	}
}

// ---- §7.2 contract params ----

func TestListDirectory_Depth(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	// depth 1 (default): docs/a.md must NOT appear.
	res, berr := tl.listDirectory(map[string]any{"path": "."})
	if berr != nil {
		t.Fatalf("depth1 failed: %v", berr)
	}
	for _, e := range res.Entries {
		if strings.ContainsRune(e.Name, filepath.Separator) || strings.Contains(e.Name, "/") {
			t.Fatalf("depth1 leaked nested entry %q", e.Name)
		}
	}
	// depth 2: docs/a.md appears with relative path.
	res2, berr := tl.listDirectory(map[string]any{"path": ".", "depth": 2})
	if berr != nil {
		t.Fatalf("depth2 failed: %v", berr)
	}
	var sawNested bool
	sep := string(filepath.Separator)
	for _, e := range res2.Entries {
		if e.Name == filepath.Join("docs", "a.md") || strings.ReplaceAll(e.Name, "/", sep) == filepath.Join("docs", "a.md") {
			sawNested = true
		}
	}
	if !sawNested {
		t.Fatalf("depth2 missing nested entry; got %v", namesOf(res2.Entries))
	}
	// depth clamped to max 3.
	res3, berr := tl.listDirectory(map[string]any{"path": ".", "depth": 99})
	if berr != nil {
		t.Fatalf("depth clamp failed: %v", berr)
	}
	_ = res3
}

func TestListDirectory_Limit(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	for i := 0; i < 10; i++ {
		must(t, os.WriteFile(filepath.Join(root, fmt.Sprintf("f%02d.txt", i)), []byte("x"), 0o644))
	}
	res, berr := tl.listDirectory(map[string]any{"path": ".", "limit": 3})
	if berr != nil {
		t.Fatalf("limit failed: %v", berr)
	}
	if len(res.Entries) != 3 {
		t.Fatalf("expected 3 entries, got %d", len(res.Entries))
	}
	// limit over contract cap clamps to 2000 (not rejected).
	res2, berr := tl.listDirectory(map[string]any{"path": ".", "limit": 999999})
	if berr != nil {
		t.Fatalf("big limit failed: %v", berr)
	}
	_ = res2
}

func TestReadFile_OffsetLength(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.WriteFile(filepath.Join(root, "window.txt"), []byte("0123456789abcdef"), 0o644))
	res, berr := tl.readFile(map[string]any{"path": "window.txt", "offset": 4, "length": 4})
	if berr != nil {
		t.Fatalf("offset/length failed: %v", berr)
	}
	if res.Text != "4567" {
		t.Fatalf("expected 4567, got %q", res.Text)
	}
	if res.Size != 16 || res.Offset != 4 || res.Bytes != 4 || !res.Truncated {
		t.Fatalf("window metadata wrong: size=%d off=%d bytes=%d trunc=%v", res.Size, res.Offset, res.Bytes, res.Truncated)
	}
	// offset beyond EOF -> empty window, no error.
	res2, berr := tl.readFile(map[string]any{"path": "window.txt", "offset": 99})
	if berr != nil {
		t.Fatalf("offset clamping failed: %v", berr)
	}
	if res2.Bytes != 0 {
		t.Fatalf("expected 0 bytes, got %d", res2.Bytes)
	}
	// offset+length lets a big file through while whole-file read stays capped.
	tl2, root2 := newTestTools(t, false, 8)
	must(t, os.WriteFile(filepath.Join(root2, "big.txt"), []byte("0123456789"), 0o644))
	res3, berr := tl2.readFile(map[string]any{"path": "big.txt", "offset": 0, "length": 4})
	if berr != nil {
		t.Fatalf("windowed read of oversized file failed: %v", berr)
	}
	if res3.Text != "0123" {
		t.Fatalf("expected 0123, got %q", res3.Text)
	}
	if _, berr := tl2.readFile(map[string]any{"path": "big.txt"}); berr == nil {
		t.Fatal("whole-file oversized read should still fail")
	}
}

func TestSearchFiles_LimitParam(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	for i := 0; i < 5; i++ {
		must(t, os.WriteFile(filepath.Join(root, fmt.Sprintf("hit%d.md", i)), []byte("x"), 0o644))
	}
	res, berr := tl.searchFiles(map[string]any{"pattern": "hit*.md", "limit": 2})
	if berr != nil {
		t.Fatalf("limit param failed: %v", berr)
	}
	if len(res.Matches) != 2 || !res.Truncated {
		t.Fatalf("expected 2 truncated, got %d trunc=%v", len(res.Matches), res.Truncated)
	}
}

func TestSearchFiles_SkipsVCSAndNonRecursive(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.MkdirAll(filepath.Join(root, ".git"), 0o755))
	must(t, os.WriteFile(filepath.Join(root, ".git", "config.md"), []byte("x"), 0o644))
	must(t, os.MkdirAll(filepath.Join(root, "deep"), 0o755))
	must(t, os.WriteFile(filepath.Join(root, "deep", "nested.md"), []byte("x"), 0o644))
	// .git contents never surface.
	res, berr := tl.searchFiles(map[string]any{"pattern": "*.md"})
	if berr != nil {
		t.Fatalf("vcs skip failed: %v", berr)
	}
	for _, m := range res.Matches {
		if strings.Contains(m.Path, ".git") {
			t.Fatalf("search leaked .git entry %q", m.Path)
		}
	}
	// recursive=false -> only immediate children.
	res2, berr := tl.searchFiles(map[string]any{"pattern": "*.md", "recursive": false})
	if berr != nil {
		t.Fatalf("non-recursive failed: %v", berr)
	}
	for _, m := range res2.Matches {
		if strings.ContainsAny(m.Path, `/\`) {
			t.Fatalf("non-recursive leaked nested %q", m.Path)
		}
	}
}

func namesOf(entries []DirEntry) []string {
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Name)
	}
	return out
}
