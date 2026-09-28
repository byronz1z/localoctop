package octobridge

import (
	"encoding/base64"
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
	cfg.ServerURL = "wss://adapter.example.com/mcp-localfs/ws"
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
