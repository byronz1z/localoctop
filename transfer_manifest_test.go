package localoctop

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"
)

// newManifestBridge builds a Bridge rooted at a fresh temp dir. The manifest
// walker is pure local filesystem work, so nothing here dials; the cfg only
// needs to validate.
func newManifestBridge(t *testing.T) (*Bridge, string) {
	t.Helper()
	root := t.TempDir()
	cfg := NewConfig()
	cfg.ServerURL = "wss://adapter.example.com/mcp/localoctop/ws"
	cfg.Token = "test-token"
	cfg.AllowedDirs = []string{root}
	cfg.Logger = NopLogger{}
	cfg.AuditLogPath = filepath.Join(root, "audit.log")
	if err := cfg.Validate(); err != nil {
		t.Fatalf("config invalid: %v", err)
	}
	b, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(b.Shutdown)
	return b, root
}

// writeTree creates a file (and its parents) under root with the given
// content and returns its absolute path.
func writeTree(t *testing.T, root, rel string, content string) string {
	t.Helper()
	p := filepath.Join(root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(p), err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", p, err)
	}
	return p
}

func TestTransferManifest_ThreeLevelsChineseAndSpaces(t *testing.T) {
	b, root := newManifestBridge(t)
	writeTree(t, root, "合同 2026/顶层.pdf", "abc")
	writeTree(t, root, "合同 2026/a b/说明 文件.txt", "hello")
	writeTree(t, root, "合同 2026/一层/二层 深/深层.txt", "1234567890")
	// A sibling outside the folder must not appear in the manifest.
	writeTree(t, root, "别的/out.txt", "nope")

	res, berr := b.transferManifest(map[string]any{"path": "合同 2026"})
	if berr != nil {
		t.Fatalf("transferManifest failed: %v", berr)
	}
	if res.Path != "合同 2026" {
		t.Fatalf("manifest path must be whitelist-relative, got %q", res.Path)
	}
	if res.DirName != "合同 2026" {
		t.Fatalf("dir_name should be the folder's own name, got %q", res.DirName)
	}
	if res.OverLimit {
		t.Fatal("a 3-file folder is not over the limit")
	}
	if res.FileLimit != maxListLimit {
		t.Fatalf("file_limit must quote the existing folder limit, got %d", res.FileLimit)
	}
	want := map[string]int64{
		"顶层.pdf":          3,
		"a b/说明 文件.txt":   5,
		"一层/二层 深/深层.txt": 10,
	}
	if len(res.Files) != len(want) {
		t.Fatalf("expected %d files, got %d: %+v", len(want), len(res.Files), res.Files)
	}
	var gotBytes int64
	for _, f := range res.Files {
		sz, ok := want[f.Rel]
		if !ok {
			t.Fatalf("unexpected entry %q in manifest", f.Rel)
		}
		if f.Size != sz {
			t.Fatalf("entry %q size = %d, want %d", f.Rel, f.Size, sz)
		}
		if f.MTime == "" || f.MTimeU == 0 {
			t.Fatalf("entry %q lacks a modification time: %+v", f.Rel, f)
		}
		gotBytes += f.Size
	}
	if res.TotalFiles != len(want) || res.TotalBytes != gotBytes {
		t.Fatalf("totals wrong: files=%d bytes=%d (want %d/%d)",
			res.TotalFiles, res.TotalBytes, len(want), gotBytes)
	}
}

func TestTransferManifest_SkipsNonRegular(t *testing.T) {
	b, root := newManifestBridge(t)
	writeTree(t, root, "folder/real.txt", "data")
	outside := writeTree(t, root, "outside/secret.txt", "secret")
	writeTree(t, root, "outside_dir/other.txt", "other")

	linkFile := filepath.Join(root, "folder", "link-to-file.txt")
	linkDir := filepath.Join(root, "folder", "link-to-dir")
	linkOut := filepath.Join(root, "folder", "link-to-outside")
	if err := os.Symlink(filepath.Join(root, "folder", "real.txt"), linkFile); err != nil {
		if runtime.GOOS == "windows" {
			t.Skipf("cannot create symlinks here: %v", err)
		}
		t.Fatalf("symlink: %v", err)
	}
	if err := os.Symlink(filepath.Join(root, "outside_dir"), linkDir); err != nil {
		t.Fatalf("symlink dir: %v", err)
	}
	if err := os.Symlink(outside, linkOut); err != nil {
		t.Fatalf("symlink outside: %v", err)
	}

	res, berr := b.transferManifest(map[string]any{"path": "folder"})
	if berr != nil {
		t.Fatalf("transferManifest failed: %v", berr)
	}
	if len(res.Files) != 1 || res.Files[0].Rel != "real.txt" {
		t.Fatalf("only the regular file may be listed, got %+v", res.Files)
	}
	if res.SkippedNonRegular != 3 {
		t.Fatalf("expected 3 skipped non-regular entries, got %d", res.SkippedNonRegular)
	}
	// Nothing behind a link was walked into: no entry carries a link prefix
	// or the outside file's name.
	for _, f := range res.Files {
		if f.Rel != "real.txt" {
			t.Fatalf("walk followed a link: %+v", f)
		}
	}
}

func TestTransferManifest_StopsOverLimit(t *testing.T) {
	b, root := newManifestBridge(t)
	dir := filepath.Join(root, "big")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < manifestMaxFiles; i++ {
		p := filepath.Join(dir, sprintf("f%04d.txt", i))
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	res, berr := b.transferManifest(map[string]any{"path": "big"})
	if berr != nil {
		t.Fatalf("transferManifest failed: %v", berr)
	}
	if !res.OverLimit {
		t.Fatalf("%d files must be over the limit of %d", manifestMaxFiles, maxListLimit)
	}
	if len(res.Files) != manifestMaxFiles {
		t.Fatalf("the walk must stop at %d files, got %d", manifestMaxFiles, len(res.Files))
	}
}

// A folder holding EXACTLY one file over the limit is the boundary case: the
// walk appends that file and runs out of entries, so the cap is noticed on
// the way out rather than at the top of the loop.
func TestTransferManifest_ExactlyOneOverLimit(t *testing.T) {
	b, root := newManifestBridge(t)
	dir := filepath.Join(root, "boundary")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < manifestMaxFiles; i++ {
		if err := os.WriteFile(filepath.Join(dir, sprintf("b%04d", i)), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	res, berr := b.transferManifest(map[string]any{"path": "boundary"})
	if berr != nil {
		t.Fatalf("transferManifest failed: %v", berr)
	}
	if !res.OverLimit {
		t.Fatalf("exactly %d files must count as over the limit", manifestMaxFiles)
	}
}

func TestTransferManifest_AtLimitIsNotOver(t *testing.T) {
	b, root := newManifestBridge(t)
	dir := filepath.Join(root, "ok")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < maxListLimit; i++ {
		if err := os.WriteFile(filepath.Join(dir, sprintf("ok%04d", i)), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	res, berr := b.transferManifest(map[string]any{"path": "ok"})
	if berr != nil {
		t.Fatalf("transferManifest failed: %v", berr)
	}
	if res.OverLimit {
		t.Fatalf("exactly %d files is the limit, not over it", maxListLimit)
	}
	if len(res.Files) != maxListLimit {
		t.Fatalf("expected %d files, got %d", maxListLimit, len(res.Files))
	}
}

func TestTransferManifest_RefusesOutsideAndTraversal(t *testing.T) {
	b, root := newManifestBridge(t)
	writeTree(t, root, "inside/f.txt", "x")

	if _, berr := b.transferManifest(map[string]any{"path": ".."}); berr == nil || berr.Code != CodeNotAllowed {
		t.Fatalf(`".." must be refused with CodeNotAllowed, got %v`, berr)
	}
	if _, berr := b.transferManifest(map[string]any{"path": "../outside"}); berr == nil || berr.Code != CodeNotAllowed {
		t.Fatalf(`"../outside" must be refused with CodeNotAllowed, got %v`, berr)
	}
	outside := t.TempDir()
	if _, berr := b.transferManifest(map[string]any{"path": outside}); berr == nil || berr.Code != CodeNotAllowed {
		t.Fatalf("an absolute path outside the whitelist must be refused, got %v", berr)
	}
	// A missing folder is a normal not-found, and a FILE is not a folder.
	if _, berr := b.transferManifest(map[string]any{"path": "inside/nope"}); berr == nil || berr.Code != CodeNotFound {
		t.Fatalf("a missing folder must be CodeNotFound, got %v", berr)
	}
	if _, berr := b.transferManifest(map[string]any{"path": "inside/f.txt"}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("a file is not a folder: expected CodeInvalidParams, got %v", berr)
	}
	if _, berr := b.transferManifest(map[string]any{"path": ""}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("a missing path must be CodeInvalidParams, got %v", berr)
	}
}

// The method must be reachable over the real wire, not just callable in
// process: dispatch routes transfer_manifest like any other bridge method.
func TestE2E_TransferManifestOverWebSocket(t *testing.T) {
	mock := newMockAdapter(t, "tok-manifest")
	b := newTestBridge(t, mock.wsURL(), "tok-manifest", false)
	root := b.cfg.AllowedDirs[0]
	writeTree(t, root, "合同 2026/一层/a.pdf", "pdf-bytes")
	writeTree(t, root, "合同 2026/b.txt", "txt")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	conn := <-mock.conns

	resp := mock.call(t, conn, 1, MethodTransferManifest, map[string]any{"path": "合同 2026"})
	if resp.Error != nil {
		t.Fatalf("transfer_manifest errored: %+v", resp.Error)
	}
	raw, _ := json.Marshal(resp.Result)
	var man TransferManifest
	if err := json.Unmarshal(raw, &man); err != nil {
		t.Fatalf("manifest did not decode: %v (%s)", err, raw)
	}
	if len(man.Files) != 2 || man.FileLimit != maxListLimit {
		t.Fatalf("unexpected manifest over the wire: %+v", man)
	}
	rels := []string{man.Files[0].Rel, man.Files[1].Rel}
	sort.Strings(rels)
	if rels[0] != "b.txt" || rels[1] != "一层/a.pdf" {
		t.Fatalf("bad relative paths over the wire: %v", rels)
	}

	// A stat probe of the folder must answer is_dir so an adapter switches to
	// the whole-folder path.
	resp = mock.call(t, conn, 2, MethodTransferUpload,
		map[string]any{"path": "合同 2026", "stat_only": true})
	if resp.Error != nil {
		t.Fatalf("folder stat probe errored: %+v", resp.Error)
	}
	raw, _ = json.Marshal(resp.Result)
	if !strings.Contains(string(raw), `"is_dir":true`) {
		t.Fatalf("folder stat probe must mark is_dir: %s", raw)
	}

	// A real upload of the folder is still refused with the old wording.
	resp = mock.call(t, conn, 3, MethodTransferUpload, map[string]any{
		"path": "合同 2026", "transfer_id": "x", "ticket": "y",
		"url_path": "/mcp/localoctop/transfer/x"})
	if resp.Error == nil || resp.Error.Code != CodeInvalidParams {
		t.Fatalf("uploading a folder must stay refused, got %+v", resp.Error)
	}
}

// The old single-file answer ("send files one at a time") must still be given
// for a REAL upload request aimed at a directory — only the stat probe is
// answered with is_dir, and that is what the adapter uses to switch to a
// whole-folder fetch.
func TestTransferUpload_DirectoryProbeVsRealRequest(t *testing.T) {
	b, root := newManifestBridge(t)
	if err := os.MkdirAll(filepath.Join(root, "adir"), 0o755); err != nil {
		t.Fatal(err)
	}
	acc, berr := b.transferUpload(map[string]any{"path": "adir", "stat_only": true})
	if berr != nil {
		t.Fatalf("stat probe of a folder must answer normally, got %v", berr)
	}
	if !acc.IsDir {
		t.Fatalf("the stat probe must mark a folder as is_dir: %+v", acc)
	}
	if _, berr := b.transferUpload(map[string]any{
		"path": "adir", "transfer_id": "t1", "ticket": "tk",
		"url_path": "/mcp/localoctop/transfer/t1"}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("a real upload of a directory must stay refused, got %v", berr)
	}
}
