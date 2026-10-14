package localoctop

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// v0.5.0 covers the official-MCP-aligned tools plus user-requested
// extensions. Handlers are exercised directly (no WebSocket) via newTestTools.

// fakeTrashBin substitutes the platform trashPath with a fake recycle bin:
// a temp directory that "recycled" items are moved into. Production code
// keeps the real platform implementation (trashImpl is only replaced for the
// duration of the test). Returns the bin directory.
func fakeTrashBin(t *testing.T) string {
	t.Helper()
	bin := t.TempDir()
	orig := trashImpl
	trashImpl = func(abs string) (bool, error) {
		name := filepath.Base(abs)
		dest := filepath.Join(bin, name)
		for i := 1; fileExists(dest); i++ {
			dest = filepath.Join(bin, fmt.Sprintf("%s.%d", name, i))
		}
		if err := os.Rename(abs, dest); err != nil {
			return false, err
		}
		return true, nil
	}
	t.Cleanup(func() { trashImpl = orig })
	return bin
}

func TestEditFile_ApplyAndDryRun(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	bin := fakeTrashBin(t) // the apply path recycles the pre-edit version
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

	// apply writes through the tmp+recycle+rename path.
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
	// The pre-edit version went to the recycle bin (fake) first.
	old, err := os.ReadFile(filepath.Join(bin, "edit.txt"))
	if err != nil || string(old) != "alpha bravo charlie\n" {
		t.Fatalf("pre-edit version not in the fake recycle bin: %q %v", old, err)
	}
	if !ar.Recycled {
		t.Fatalf("result must report the recycled previous version: %+v", ar)
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

func TestDeleteFile_PermanentIgnoredGoesToRecycleBin(t *testing.T) {
	// permanent is no longer a supported parameter: a caller that still sends
	// permanent=true gets the recycle bin anyway, with a note saying the
	// permanent path is gone.
	tl, root := newTestTools(t, true, 0)
	bin := fakeTrashBin(t)
	must(t, os.WriteFile(filepath.Join(root, "gone.txt"), []byte("x"), 0o644))
	res, berr := tl.deleteFile(map[string]any{"path": "gone.txt", "permanent": true})
	if berr != nil {
		t.Fatalf("delete failed: %v", berr)
	}
	if !res.Deleted || !res.Recycle {
		t.Fatalf("expected a recycle-bin delete: %+v", res)
	}
	if !strings.Contains(res.Note, "permanent") || !strings.Contains(res.Note, "recycle bin") {
		t.Fatalf("result must state permanent delete is unavailable and the bin was used: %+v", res)
	}
	if _, err := os.Stat(filepath.Join(root, "gone.txt")); !os.IsNotExist(err) {
		t.Fatalf("file should be gone from its original place")
	}
	// …but recoverable: it sits in the (fake) recycle bin.
	if data, err := os.ReadFile(filepath.Join(bin, "gone.txt")); err != nil || string(data) != "x" {
		t.Fatalf("deleted file must be in the recycle bin, got %q %v", data, err)
	}
}

func TestDeleteFile_Recycled(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	bin := fakeTrashBin(t)
	must(t, os.WriteFile(filepath.Join(root, "gone.txt"), []byte("x"), 0o644))
	res, berr := tl.deleteFile(map[string]any{"path": "gone.txt"})
	if berr != nil {
		t.Fatalf("delete failed: %v", berr)
	}
	if !res.Deleted || !res.Recycle || res.Note != "" {
		t.Fatalf("expected a plain recycle-bin delete: %+v", res)
	}
	if _, err := os.ReadFile(filepath.Join(bin, "gone.txt")); err != nil {
		t.Fatalf("deleted file must be in the recycle bin: %v", err)
	}
}

func TestDeleteFile_RecycleRefusedWithoutBin(t *testing.T) {
	// On non-Windows the trash stub reports no recycle bin; removeOne must
	// refuse rather than silently hard-delete.
	if runtime.GOOS == "windows" {
		t.Skip("windows offers a real recycle bin; refusal path not exercised")
	}
	tl, root := newTestTools(t, true, 0)
	p := filepath.Join(root, "keep.txt")
	must(t, os.WriteFile(p, []byte("x"), 0o644))
	st0, err := os.Stat(p)
	must(t, err)
	_, berr := tl.deleteFile(map[string]any{"path": "keep.txt"})
	if berr == nil {
		t.Fatalf("expected refusal on platform without recycle bin")
	}
	if berr.Code != CodeInvalidParams {
		t.Fatalf("expected CodeInvalidParams, got %d", berr.Code)
	}
	if !strings.Contains(berr.Message, "recycle bin") || !strings.Contains(berr.Message, "nothing was deleted") {
		t.Fatalf("refusal must explain the missing recycle bin and that nothing was deleted: %q", berr.Message)
	}
	// The file must survive the refused delete: content and mtime unchanged.
	data, err := os.ReadFile(p)
	must(t, err)
	st1, err := os.Stat(p)
	must(t, err)
	if string(data) != "x" || !st1.ModTime().Equal(st0.ModTime()) {
		t.Fatalf("refused delete must not touch the file: %q %v %v", data, st0.ModTime(), st1.ModTime())
	}
}

func TestRemoveDirectory_Recycled(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	bin := fakeTrashBin(t)
	res, berr := tl.removeDirectory(map[string]any{"path": "docs", "recursive": true})
	if berr != nil {
		t.Fatalf("recursive remove failed: %v", berr)
	}
	if !res.Deleted || !res.Recycle {
		t.Fatalf("expected a recycle-bin remove: %+v", res)
	}
	if res.Entries < 3 { // dir + a.md + b.md
		t.Fatalf("expected entry count >=3, got %d", res.Entries)
	}
	if _, err := os.Stat(filepath.Join(root, "docs")); !os.IsNotExist(err) {
		t.Fatalf("directory should be gone from its original place")
	}
	if _, err := os.Stat(filepath.Join(bin, "docs", "a.md")); err != nil {
		t.Fatalf("removed directory contents must be in the recycle bin: %v", err)
	}
}

func TestRemoveDirectory_NonEmptyGuard(t *testing.T) {
	tl, _ := newTestTools(t, true, 0)
	fakeTrashBin(t) // a working bin so the recursive step can succeed
	if _, berr := tl.removeDirectory(map[string]any{"path": "docs"}); berr == nil {
		t.Fatalf("expected refusal removing a non-empty dir without recursive")
	}
	res, berr := tl.removeDirectory(map[string]any{"path": "docs", "recursive": true})
	if berr != nil {
		t.Fatalf("recursive remove failed: %v", berr)
	}
	if res.Entries < 3 { // dir + a.md + b.md
		t.Fatalf("expected entry count >=3, got %d", res.Entries)
	}
}

func TestRemoveDirectory_RefusedWithoutBin(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("windows offers a real recycle bin; refusal path not exercised")
	}
	tl, root := newTestTools(t, true, 0)
	p := filepath.Join(root, "docs", "a.md")
	st0, err := os.Stat(p)
	must(t, err)
	if _, berr := tl.removeDirectory(map[string]any{"path": "docs", "recursive": true}); berr == nil {
		t.Fatalf("expected refusal on platform without recycle bin")
	} else if !strings.Contains(berr.Message, "nothing was deleted") {
		t.Fatalf("refusal must state nothing was deleted: %q", berr.Message)
	}
	st1, err := os.Stat(p)
	must(t, err)
	if !st1.ModTime().Equal(st0.ModTime()) {
		t.Fatalf("refused remove must not touch the directory contents")
	}
	if _, err := os.Stat(filepath.Join(root, "docs")); err != nil {
		t.Fatalf("directory must survive the refused remove: %v", err)
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

// makeZip builds a small archive at path with the given member->content map.
func makeZip(t *testing.T, path string, members map[string]string) {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, body := range members {
		w, err := zw.Create(name)
		must(t, err)
		_, _ = w.Write([]byte(body))
	}
	must(t, zw.Close())
	must(t, os.WriteFile(path, buf.Bytes(), 0o644))
}

// TestUnzip_OverwriteRecyclesOld: overwrite=true replacing two existing files
// sends both old versions to the recycle bin before extracting the new ones.
func TestUnzip_OverwriteRecyclesOld(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	bin := fakeTrashBin(t)
	dest := filepath.Join(root, "landing")
	must(t, os.MkdirAll(dest, 0o755))
	must(t, os.WriteFile(filepath.Join(dest, "one.txt"), []byte("OLD-ONE"), 0o644))
	must(t, os.WriteFile(filepath.Join(dest, "two.txt"), []byte("OLD-TWO"), 0o644))
	makeZip(t, filepath.Join(root, "pkg.zip"), map[string]string{"one.txt": "NEW-ONE", "two.txt": "NEW-TWO"})

	ur, berr := tl.unzipFile(map[string]any{"archive": "pkg.zip", "dest": "landing", "overwrite": true})
	if berr != nil {
		t.Fatalf("overwrite unzip failed: %v", berr)
	}
	if ur.Extracted != 2 || len(ur.Skipped) != 0 {
		t.Fatalf("expected 2 extracted, none skipped: %+v", ur)
	}
	if data, _ := os.ReadFile(filepath.Join(dest, "one.txt")); string(data) != "NEW-ONE" {
		t.Fatalf("one.txt not overwritten: %q", data)
	}
	if data, _ := os.ReadFile(filepath.Join(dest, "two.txt")); string(data) != "NEW-TWO" {
		t.Fatalf("two.txt not overwritten: %q", data)
	}
	// Both old versions are recoverable in the (fake) recycle bin.
	if old, err := os.ReadFile(filepath.Join(bin, "one.txt")); err != nil || string(old) != "OLD-ONE" {
		t.Fatalf("old one.txt not in recycle bin: %q %v", old, err)
	}
	if old, err := os.ReadFile(filepath.Join(bin, "two.txt")); err != nil || string(old) != "OLD-TWO" {
		t.Fatalf("old two.txt not in recycle bin: %q %v", old, err)
	}
}

// TestUnzip_OverwriteRefusedWithoutBin: with no recycle bin, overwrite skips
// the colliding members (originals untouched, content AND mtime unchanged) and
// lists them; a member with no collision still extracts.
func TestUnzip_OverwriteRefusedWithoutBin(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("windows offers a real recycle bin; refusal path not exercised")
	}
	tl, root := newTestTools(t, true, 0)
	dest := filepath.Join(root, "landing")
	must(t, os.MkdirAll(dest, 0o755))
	// one.txt collides; fresh.txt does not.
	old := time.Now().Add(-2 * time.Hour)
	must(t, os.WriteFile(filepath.Join(dest, "one.txt"), []byte("OLD-ONE"), 0o644))
	must(t, os.Chtimes(filepath.Join(dest, "one.txt"), old, old))
	makeZip(t, filepath.Join(root, "pkg.zip"), map[string]string{"one.txt": "NEW-ONE", "fresh.txt": "NEW-FRESH"})

	ur, berr := tl.unzipFile(map[string]any{"archive": "pkg.zip", "dest": "landing", "overwrite": true})
	if berr != nil {
		t.Fatalf("unzip failed: %v", berr)
	}
	if len(ur.Skipped) != 1 || !strings.Contains(ur.Skipped[0], "one.txt") {
		t.Fatalf("colliding member must be reported skipped: %+v", ur)
	}
	if ur.Extracted != 1 { // only fresh.txt
		t.Fatalf("expected only the non-colliding member extracted, got %d", ur.Extracted)
	}
	// The skipped member's original is untouched: content and mtime unchanged.
	data, err := os.ReadFile(filepath.Join(dest, "one.txt"))
	must(t, err)
	st, err := os.Stat(filepath.Join(dest, "one.txt"))
	must(t, err)
	if string(data) != "OLD-ONE" || !st.ModTime().Equal(old) {
		t.Fatalf("skipped member must be untouched: %q %v", data, st.ModTime())
	}
	// The fresh member did land.
	if nf, err := os.ReadFile(filepath.Join(dest, "fresh.txt")); err != nil || string(nf) != "NEW-FRESH" {
		t.Fatalf("non-colliding member should have extracted: %q %v", nf, err)
	}
}

// TestEditFile_RenameFailsAfterRecycle: the recycle succeeds but the tmp->abs
// rename fails. The temp file must be cleaned up and the error must say the
// original is now in the recycle bin (with how to restore it).
func TestEditFile_RenameFailsAfterRecycle(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	bin := t.TempDir()
	orig := trashImpl
	p := filepath.Join(root, "edit.txt")
	must(t, os.WriteFile(p, []byte("alpha bravo charlie\n"), 0o644))
	// Fake bin that recycles the original, then plants a non-empty directory at
	// the original name so os.Rename(tmp, abs) fails on every platform.
	trashImpl = func(abs string) (bool, error) {
		name := filepath.Base(abs)
		if err := os.Rename(abs, filepath.Join(bin, name)); err != nil {
			return false, err
		}
		must(t, os.MkdirAll(abs, 0o755))
		must(t, os.WriteFile(filepath.Join(abs, "blocker"), []byte("x"), 0o644))
		return true, nil
	}
	t.Cleanup(func() { trashImpl = orig })

	_, berr := tl.editFile(map[string]any{
		"path":  "edit.txt",
		"edits": []any{map[string]any{"oldText": "bravo", "newText": "BETA"}},
	})
	if berr == nil {
		t.Fatalf("expected the rename-after-recycle failure to surface")
	}
	if berr.Code != CodeInternal {
		t.Fatalf("expected CodeInternal, got %d", berr.Code)
	}
	if !strings.Contains(berr.Message, "recycle bin") || !strings.Contains(berr.Message, "restore") {
		t.Fatalf("error must say the original is in the recycle bin and how to restore it: %q", berr.Message)
	}
	// The temp file must not be left behind.
	if _, err := os.Stat(p + ".tmp-localoctop"); !os.IsNotExist(err) {
		t.Fatalf("temp file left behind after failure: %v", err)
	}
	// The original content is safe in the bin.
	if data, err := os.ReadFile(filepath.Join(bin, "edit.txt")); err != nil || string(data) != "alpha bravo charlie\n" {
		t.Fatalf("original must be recoverable in the recycle bin: %q %v", data, err)
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

// ---- T5-3: error texts never carry an absolute path of this machine ----

func TestUnzip_ConflictErrorHasNoAbsolutePath(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	fakeTrashBin(t)
	dest := filepath.Join(root, "landing")
	must(t, os.MkdirAll(dest, 0o755))
	must(t, os.WriteFile(filepath.Join(dest, "a.txt"), []byte("OLD"), 0o644))
	makeZip(t, filepath.Join(root, "pkg.zip"), map[string]string{"a.txt": "NEW"})

	_, berr := tl.unzipFile(map[string]any{"archive": "pkg.zip", "dest": "landing"})
	if berr == nil {
		t.Fatal("expected a conflict refusal without overwrite")
	}
	msg := berr.Message
	if !strings.Contains(msg, "member target already exists") {
		t.Fatalf("conflict message lost its cause: %q", msg)
	}
	// The whitelist-relative name is fine; the machine's layout is not.
	if !strings.Contains(msg, "landing/a.txt") {
		t.Fatalf("the relative path must stay readable: %q", msg)
	}
	if strings.Contains(msg, root) || strings.Contains(msg, filepath.ToSlash(root)) {
		t.Fatalf("error text leaks the machine's absolute path: %q", msg)
	}
	for _, marker := range []string{`D:\`, `C:\`, "D:/", "C:/", "/Users/", "/home/"} {
		if strings.Contains(msg, marker) {
			t.Fatalf("error text leaks an absolute path marker %q: %q", marker, msg)
		}
	}
}

// ---- T5-5b: unzip overwrite reports which old versions went to the bin ----

func TestUnzip_OverwriteListsRecycledMembers(t *testing.T) {
	tl, root := newTestTools(t, true, 0)
	bin := fakeTrashBin(t)
	dest := filepath.Join(root, "landing")
	must(t, os.MkdirAll(dest, 0o755))
	must(t, os.WriteFile(filepath.Join(dest, "one.txt"), []byte("OLD-ONE"), 0o644))
	makeZip(t, filepath.Join(root, "pkg.zip"),
		map[string]string{"one.txt": "NEW-ONE", "fresh.txt": "FRESH"})

	ur, berr := tl.unzipFile(map[string]any{"archive": "pkg.zip", "dest": "landing", "overwrite": true})
	if berr != nil {
		t.Fatalf("overwrite unzip failed: %v", berr)
	}
	if ur.Extracted != 2 {
		t.Fatalf("expected 2 extracted, got %+v", ur)
	}
	if len(ur.Recycled) != 1 || ur.Recycled[0] != "landing/one.txt" {
		t.Fatalf("the overwritten member's old version must be named as recycled: %+v", ur.Recycled)
	}
	if _, err := os.ReadFile(filepath.Join(bin, "one.txt")); err != nil {
		t.Fatalf("old one.txt must be in the recycle bin: %v", err)
	}
}

// ---- T5-4: a listing/search that is not complete says so ----------------

func TestListDirectory_ReportsTruncationAndLowerBound(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	// A folder with more entries than the listing cap.
	big := filepath.Join(root, "big")
	must(t, os.MkdirAll(big, 0o755))
	for i := 0; i < 30; i++ {
		must(t, os.WriteFile(filepath.Join(big, sprintf("f%03d.txt", i)), []byte("x"), 0o644))
	}
	res, berr := tl.listDirectory(map[string]any{"path": "big", "limit": 10})
	if berr != nil {
		t.Fatalf("list failed: %v", berr)
	}
	if len(res.Entries) != 10 || !res.Truncated {
		t.Fatalf("expected 10 entries + truncated flag, got %d entries truncated=%v",
			len(res.Entries), res.Truncated)
	}
	// The cap is never the total: the lower bound must exceed it.
	if res.TotalKnown || res.TotalEntries <= 10 {
		t.Fatalf("truncated listing must not claim the cap as the total: %+v", res)
	}
	if res.Limit != 10 {
		t.Fatalf("the applied limit must ride along, got %d", res.Limit)
	}
}

func TestListDirectory_ExactTotalWhenComplete(t *testing.T) {
	tl, _ := newTestTools(t, false, 0)
	res, berr := tl.listDirectory(map[string]any{"path": "."})
	if berr != nil {
		t.Fatalf("list failed: %v", berr)
	}
	if res.Truncated || !res.TotalKnown || res.TotalEntries != len(res.Entries) {
		t.Fatalf("a complete listing must report the exact total: %+v", res)
	}
}

func TestListDirWithSizes_NeverPresentsCapAsTotal(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	big := filepath.Join(root, "big")
	must(t, os.MkdirAll(big, 0o755))
	// The real cap is 5000 (too slow to fill here); the wiring under test is
	// the flag plumbing, and collectDirEntries is shared with the capped
	// paths, so a small folder proves the flags on the complete side.
	for i := 0; i < 12; i++ {
		must(t, os.WriteFile(filepath.Join(big, sprintf("f%02d.txt", i)), []byte("x"), 0o644))
	}
	res, berr := tl.listDirWithSizes(map[string]any{"path": "big"})
	if berr != nil {
		t.Fatalf("list_with_sizes failed: %v", berr)
	}
	// Under the cap: totals count everything and say so.
	if res.Truncated || !res.TotalsCountAll || res.TotalFiles != 12 {
		t.Fatalf("a full walk must mark its totals complete: %+v", res)
	}
	if res.TotalEntriesSeen != 12 {
		t.Fatalf("seen-count wrong: %+v", res)
	}
}

func TestListDirWithSizes_CappedWalkIsMarked(t *testing.T) {
	// Drive collectDirEntries past a small cap directly: truncated must be
	// set and the totals must be marked as NOT counting everything.
	root := t.TempDir()
	for i := 0; i < 25; i++ {
		must(t, os.WriteFile(filepath.Join(root, sprintf("f%02d.txt", i)), []byte("x"), 0o644))
	}
	entries, truncated, total, err := collectDirEntries(root, root, 1, 1, 5)
	must(t, err)
	if len(entries) != 5 || !truncated {
		t.Fatalf("expected 5 entries + truncated, got %d/%v", len(entries), truncated)
	}
	if total != 25 {
		t.Fatalf("total must count every entry seen (lower bound would be wrong here), got %d", total)
	}
}

// TestListDirectory_SixThousandFiles: the tester's real case — a 6000-file
// folder through BOTH listing tools must come back marked incomplete with a
// truthful count, never with the cap presented as the total.
func TestListDirectory_SixThousandFiles(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	big := filepath.Join(root, "big")
	must(t, os.MkdirAll(big, 0o755))
	for i := 0; i < 6000; i++ {
		must(t, os.WriteFile(filepath.Join(big, sprintf("f%05d.txt", i)), []byte("x"), 0o644))
	}

	// list_directory at its maximum limit: 2000 shown of 6000, marked
	// truncated, lower bound above the cap.
	res, berr := tl.listDirectory(map[string]any{"path": "big", "limit": 2000})
	if berr != nil {
		t.Fatalf("list failed: %v", berr)
	}
	if len(res.Entries) != 2000 || !res.Truncated || res.TotalKnown {
		t.Fatalf("6000-file folder must truncate at the 2000 cap: entries=%d %+v",
			len(res.Entries), res)
	}
	if res.TotalEntries <= 2000 {
		t.Fatalf("the lower bound must exceed the cap (the folder has 6000): %d", res.TotalEntries)
	}

	// list_directory_with_sizes: the internal 5000 cap must NOT surface as
	// the folder's totals.
	ws, berr := tl.listDirWithSizes(map[string]any{"path": "big"})
	if berr != nil {
		t.Fatalf("list_with_sizes failed: %v", berr)
	}
	if !ws.Truncated || ws.TotalsCountAll {
		t.Fatalf("capped walk must be marked incomplete: %+v", ws)
	}
	if ws.TotalFiles != 5000 {
		t.Fatalf("totals count the 5000 listed entries, got %d", ws.TotalFiles)
	}
	if ws.TotalEntriesSeen <= 5000 {
		t.Fatalf("the seen lower bound must exceed the cap, got %d", ws.TotalEntriesSeen)
	}
}

func TestSearchFiles_ContentSearchNamesSkippedLargeFiles(t *testing.T) {
	tl, root := newTestTools(t, false, 1024) // tiny read cap: 1 KB
	// Both files match the NAME pattern; the big one's content would match
	// too, but it is over the read cap — silently skipping it made
	// "0 matches" a lie (the tester's 25 MB log case).
	big := strings.Repeat("pad\n", 400) + "needle\n"
	must(t, os.WriteFile(filepath.Join(root, "needle-big.log"), []byte(big), 0o644))
	must(t, os.WriteFile(filepath.Join(root, "needle-small.log"), []byte("nothing here\n"), 0o644))

	res, berr := tl.searchFiles(map[string]any{
		"path": ".", "pattern": "needle", "match_content": true, "include_dirs": false})
	if berr != nil {
		t.Fatalf("search failed: %v", berr)
	}
	// match_content is "name OR content" (G2 batch-2): both files match by
	// name, so both are hits; the big one is ALSO named as not-searched-inside.
	if len(res.Matches) != 2 {
		t.Fatalf("both name hits must be found: %+v", res.Matches)
	}
	if res.SkippedLarge != 1 {
		t.Fatalf("the skipped large file must be counted: %+v", res)
	}
	if len(res.SkippedLargeNames) != 1 || res.SkippedLargeNames[0] != "needle-big.log" {
		t.Fatalf("the skipped large file must be named: %+v", res.SkippedLargeNames)
	}
	if res.SkippedLargeLimit != 1024 {
		t.Fatalf("the result must state the skip line (1 KB), got %d", res.SkippedLargeLimit)
	}
}
