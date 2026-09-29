package localoctop

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// v0.5.0 covers the official-MCP-aligned tools plus user-requested
// extensions. Handlers are exercised directly (no WebSocket) via newTestTools.

func TestEditFile_ApplyAndDryRun(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	p := filepath.Join(root, "edit.txt")
	must(t, os.WriteFile(p, []byte("alpha bravo charlie\n"), 0o644))

	// dryRun reports the diff but leaves the file untouched.
	dr, berr := tl.editFile(map[string]any{
		"path":   "edit.txt",
		"dryRun": true,
		"edits":  []any{map[string]any{"oldText": "bravo", "newText": "BETA"}},
	})
	if berr != nil {
		t.Fatalf("dryRun edit failed: %v", berr)
	}
	if dr.Applied || !dr.DryRun || dr.EditsApplied != 1 {
		t.Fatalf("unexpected dryRun result: %+v", dr)
	}
	if onDisk, _ := os.ReadFile(p); string(onDisk) != "alpha bravo charlie\n" {
		t.Fatalf("dryRun modified the file: %q", onDisk)
	}

	// apply writes through the tmp+rename path.
	ar, berr := tl.editFile(map[string]any{
		"path":  "edit.txt",
		"edits": []any{map[string]any{"oldText": "bravo", "newText": "BETA"}},
	})
	if berr != nil {
		t.Fatalf("apply edit failed: %v", berr)
	}
	if !ar.Applied || ar.DryRun {
		t.Fatalf("expected applied edit: %+v", ar)
	}
	if onDisk, _ := os.ReadFile(p); string(onDisk) != "alpha BETA charlie\n" {
		t.Fatalf("file not edited: %q", onDisk)
	}
}

func TestEditFile_DuplicateAndMissingRejected(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	must(t, os.WriteFile(filepath.Join(root, "dup.txt"), []byte("x x x\n"), 0o644))
	if _, berr := tl.editFile(map[string]any{
		"path":  "dup.txt",
		"edits": []any{map[string]any{"oldText": "x", "newText": "y"}},
	}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("expected ambiguity rejection, got %v", berr)
	}
	if _, berr := tl.editFile(map[string]any{
		"path":  "dup.txt",
		"edits": []any{map[string]any{"oldText": "nope", "newText": "y"}},
	}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("expected not-found rejection, got %v", berr)
	}
}

func TestEditFile_WriteGate(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.WriteFile(filepath.Join(root, "g.txt"), []byte("a\n"), 0o644))
	if _, berr := tl.editFile(map[string]any{
		"path":  "g.txt",
		"edits": []any{map[string]any{"oldText": "a", "newText": "b"}},
	}); berr == nil || berr.Code != CodeWriteDisabled {
		t.Fatalf("expected write-disabled, got %v", berr)
	}
}

func TestReadMediaFile(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	raw := []byte{0x00, 0x01, 0x02, 0xff, 0xfe}
	must(t, os.WriteFile(filepath.Join(root, "pic.bin"), raw, 0o644))
	res, berr := tl.readMediaFile(map[string]any{"path": "pic.bin"})
	if berr != nil {
		t.Fatalf("read_media_file failed: %v", berr)
	}
	dec, derr := base64.StdEncoding.DecodeString(res.Base64)
	if derr != nil || !bytes.Equal(dec, raw) {
		t.Fatalf("media round-trip mismatch: %v %x", derr, dec)
	}
	if res.Size != int64(len(raw)) {
		t.Fatalf("size wrong: %d", res.Size)
	}
}

func TestReadMediaFile_TooLarge(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	tl.cfg.MaxMediaBytes = 4
	must(t, os.WriteFile(filepath.Join(root, "big.bin"), []byte("123456789"), 0o644))
	if _, berr := tl.readMediaFile(map[string]any{"path": "big.bin"}); berr == nil || berr.Code != CodeTooLarge {
		t.Fatalf("expected too-large, got %v", berr)
	}
}

func TestReadMultipleFiles_PartialFailure(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	// hello.txt exists; "ghost.txt" missing; "docs" is a directory.
	res, berr := tl.readMultipleFiles(map[string]any{
		"paths": []any{"hello.txt", "ghost.txt", "docs"},
	})
	if berr != nil {
		t.Fatalf("read_multiple_files failed: %v", berr)
	}
	if len(res.Files) != 3 {
		t.Fatalf("expected 3 entries, got %d", len(res.Files))
	}
	if res.Failed != 2 {
		t.Fatalf("expected 2 failures, got %d", res.Failed)
	}
	var ok bool
	for _, f := range res.Files {
		if f.Path == "hello.txt" && f.Text == "hi there\n" {
			ok = true
		}
	}
	if !ok {
		t.Fatalf("hello.txt not returned intact: %+v", res.Files)
	}
}

func TestListDirWithSizes(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.listDirWithSizes(map[string]any{"path": "."})
	if berr != nil {
		t.Fatalf("list_with_sizes failed: %v", berr)
	}
	if res.TotalDirs == 0 || res.TotalFiles == 0 {
		t.Fatalf("expected both counts > 0: %+v", res)
	}
}

func TestDirectoryTree(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.directoryTree(map[string]any{"path": "."})
	if berr != nil {
		t.Fatalf("directory_tree failed: %v", berr)
	}
	var docsFound bool
	for _, n := range res.Tree {
		if n.Name == "docs" && n.Type == "directory" && len(n.Children) == 2 {
			docsFound = true
		}
	}
	if !docsFound {
		t.Fatalf("docs subtree not built correctly: %+v", res.Tree)
	}
}

func TestMoveFile(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	if _, berr := tl.moveFile(map[string]any{"source": "hello.txt", "destination": "moved.txt"}); berr != nil {
		t.Fatalf("move failed: %v", berr)
	}
	if _, err := os.Stat(filepath.Join(root, "moved.txt")); err != nil {
		t.Fatalf("destination missing after move: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "hello.txt")); !os.IsNotExist(err) {
		t.Fatalf("source should be gone")
	}
	// Destination already exists → refused (never overwrite).
	if _, berr := tl.moveFile(map[string]any{"source": "moved.txt", "destination": "docs"}); berr == nil {
		t.Fatalf("expected refusal moving onto existing destination")
	}
}

func TestMoveFile_WriteGate(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	if _, berr := tl.moveFile(map[string]any{"source": "hello.txt", "destination": "x.txt"}); berr == nil || berr.Code != CodeWriteDisabled {
		t.Fatalf("expected write-disabled, got %v", berr)
	}
}

func TestDeleteFile_Permanent(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	must(t, os.WriteFile(filepath.Join(root, "gone.txt"), []byte("x"), 0o644))
	res, berr := tl.deleteFile(map[string]any{"path": "gone.txt", "permanent": true})
	if berr != nil {
		t.Fatalf("permanent delete failed: %v", berr)
	}
	if !res.Deleted || !res.Permanent {
		t.Fatalf("unexpected result: %+v", res)
	}
	if _, err := os.Stat(filepath.Join(root, "gone.txt")); !os.IsNotExist(err) {
		t.Fatalf("file should be deleted")
	}
}

func TestDeleteFile_RecycleRefusedWithoutBin(t *testing.T) {
	// On non-Windows the trash stub reports no recycle bin; removeOne must
	// refuse rather than silently hard-delete.
	if runtime.GOOS == "windows" {
		t.Skip("windows offers a real recycle bin; refusal path not exercised")
	}
	tl, root := newTestTools(t, true, 0)
	must(t, os.WriteFile(filepath.Join(root, "keep.txt"), []byte("x"), 0o644))
	if _, berr := tl.deleteFile(map[string]any{"path": "keep.txt"}); berr == nil {
		t.Fatalf("expected refusal on platform without recycle bin")
	}
	// The file must survive the refused delete.
	if _, err := os.Stat(filepath.Join(root, "keep.txt")); err != nil {
		t.Fatalf("refused delete must not remove the file: %v", err)
	}
}

func TestRemoveDirectory_NonEmptyGuard(t *testing.T) {
	tl, _ := newTestTools(t, true, 0)
	if _, berr := tl.removeDirectory(map[string]any{"path": "docs"}); berr == nil {
		t.Fatalf("expected refusal removing a non-empty dir without recursive")
	}
	res, berr := tl.removeDirectory(map[string]any{"path": "docs", "recursive": true, "permanent": true})
	if berr != nil {
		t.Fatalf("recursive permanent remove failed: %v", berr)
	}
	if res.Entries < 3 { // dir + a.md + b.md
		t.Fatalf("expected entry count >=3, got %d", res.Entries)
	}
}

func TestRemoveDirectory_WhitelistRootRefused(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	if _, berr := tl.removeDirectory(map[string]any{"path": root, "permanent": true}); berr == nil {
		t.Fatalf("refusing to remove whitelist root failed")
	}
}

func TestZipUnzip_RoundTrip(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	zr, berr := tl.zipFiles(map[string]any{"paths": []any{"docs", "hello.txt"}})
	if berr != nil {
		t.Fatalf("zip_files failed: %v", berr)
	}
	if zr.Entries < 3 { // docs/a.md, docs/b.md, hello.txt
		t.Fatalf("expected >=3 zip entries, got %d", zr.Entries)
	}
	// Write the archive into the whitelist, then unzip to a fresh dest.
	raw, derr := base64.StdEncoding.DecodeString(zr.Base64)
	if derr != nil {
		t.Fatalf("zip base64 decode: %v", derr)
	}
	must(t, os.WriteFile(filepath.Join(root, "bundle.zip"), raw, 0o644))
	ur, berr := tl.unzipFile(map[string]any{"archive": "bundle.zip", "dest": "out"})
	if berr != nil {
		t.Fatalf("unzip_file failed: %v", berr)
	}
	if ur.Extracted != zr.Entries {
		t.Fatalf("unzip extracted %d != zipped %d", ur.Extracted, zr.Entries)
	}
	if _, err := os.Stat(filepath.Join(root, "out", "docs", "a.md")); err != nil {
		t.Fatalf("round-tripped file missing: %v", err)
	}
}

func TestUnzip_ZipSlipRefused(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	// Craft an archive whose member escapes the destination.
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, err := zw.Create("../escape.txt")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = w.Write([]byte("bad"))
	_ = zw.Close()
	must(t, os.WriteFile(filepath.Join(root, "evil.zip"), buf.Bytes(), 0o644))
	if _, berr := tl.unzipFile(map[string]any{"archive": "evil.zip", "dest": "out2"}); berr == nil || berr.Code != CodeNotAllowed {
		t.Fatalf("expected zip-slip refusal, got %v", berr)
	}
	if _, err := os.Stat(filepath.Join(root, "..", "escape.txt")); err == nil {
		t.Fatalf("zip slip wrote outside destination!")
	}
}

func TestUnzip_WriteGate(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.WriteFile(filepath.Join(root, "a.zip"), []byte("not a zip"), 0o644))
	if _, berr := tl.unzipFile(map[string]any{"archive": "a.zip", "dest": "out"}); berr == nil || berr.Code != CodeWriteDisabled {
		t.Fatalf("expected write-disabled, got %v", berr)
	}
}

func TestListAllowedDirectories(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	res, berr := tl.listAllowedDirectories(nil)
	if berr != nil {
		t.Fatalf("list_allowed_directories failed: %v", berr)
	}
	if len(res.Directories) != 1 {
		t.Fatalf("expected 1 root, got %d", len(res.Directories))
	}
	if res.Directories[0] == "" {
		t.Fatalf("root not reported: %+v (root=%s)", res.Directories, root)
	}
}

func TestSearchFilesRegex_NameAndContent(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	// name-based: match .md files
	nameRes, berr := tl.searchFilesV05(map[string]any{"path": ".", "pattern": `\.md$`, "mode": "regex"})
	if berr != nil {
		t.Fatalf("regex name search failed: %v", berr)
	}
	if len(nameRes.Matches) != 2 {
		t.Fatalf("expected 2 .md matches, got %d: %+v", len(nameRes.Matches), nameRes.Matches)
	}
	// content-based: "banana" lives in docs/b.md
	must(t, os.WriteFile(filepath.Join(root, "notes.txt"), []byte("line1\nbanana split\nline3\n"), 0o644))
	cRes, berr := tl.searchFilesV05(map[string]any{
		"path": ".", "pattern": "banana", "mode": "regex", "match_content": true, "include_dirs": false,
	})
	if berr != nil {
		t.Fatalf("regex content search failed: %v", berr)
	}
	var hitLine int
	for _, m := range cRes.Matches {
		if m.Path == "notes.txt" {
			hitLine = m.Line
		}
	}
	if hitLine != 2 {
		t.Fatalf("expected banana at line 2 of notes.txt, got %d (%+v)", hitLine, cRes.Matches)
	}
}
