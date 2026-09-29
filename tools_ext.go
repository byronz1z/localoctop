package localoctop

// v0.5.0 tool set aligned with the official MCP filesystem server plus
// user-requested extensions (delete with recycle-bin default, zip/unzip,
// regex search). Every method resolves paths through the guard; write-class
// methods refuse while cfg.AllowWrite=false.

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

// ---- limits specific to the v0.5.0 additions ----

const (
	maxTreeNodes         = 20000 // directory_tree node cap
	maxMultiFiles        = 50    // read_multiple_files batch cap
	maxZipEntries        = 5000  // zip_files / unzip_file member cap
	maxEditBytesPerFile  = 4 << 20
	maxMediaBytesDefault = DefaultMaxMediaBytes
)

// resolveAny resolves a raw path and requires it to exist.
func (t *tools) resolveAny(raw string) (abs string, berr *BridgeError) {
	if raw == "" {
		return "", errInvalidParams("path is required")
	}
	_, abs, berr = t.guard.Resolve(raw)
	return abs, berr
}

// ---- read_media_file ----

func (t *tools) readMediaFile(params map[string]any) (*MediaFileResult, *BridgeError) {
	abs, berr := t.resolveAny(getString(params, "path", ""))
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("file not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("stat failed", err)
	}
	if st.IsDir() {
		return nil, errInvalidParams("path is a directory")
	}
	mediaCap := t.cfg.MaxMediaBytes
	if mediaCap <= 0 {
		mediaCap = maxMediaBytesDefault
	}
	if st.Size() > mediaCap {
		return nil, errTooLarge(sprintf("media file is %d bytes, exceeds one-shot media limit %d; use zip_files or windowed read_file", st.Size(), mediaCap))
	}
	data, err := os.ReadFile(abs) // #nosec G304 - path validated by guard
	if err != nil {
		return nil, errInternal("read failed", err)
	}
	return &MediaFileResult{
		Path:   relDisplay(t.cfg.AllowedDirs, abs),
		Base64: base64.StdEncoding.EncodeToString(data),
		MIME:   guessMIME(st.Name(), false),
		Size:   st.Size(),
	}, nil
}

// ---- read_multiple_files ----

func (t *tools) readMultipleFiles(params map[string]any) (*ReadMultipleResult, *BridgeError) {
	rawPaths, _ := params["paths"].([]any)
	if len(rawPaths) == 0 {
		return nil, errInvalidParams("paths (array of strings) is required")
	}
	if len(rawPaths) > maxMultiFiles {
		rawPaths = rawPaths[:maxMultiFiles]
	}
	res := &ReadMultipleResult{Files: make([]MultiFileEntry, 0, len(rawPaths))}
	var total int64
	cap := t.cfg.MaxReadBytes
	if cap <= 0 {
		cap = DefaultMaxReadBytes
	}
	for _, rp := range rawPaths {
		s, _ := rp.(string)
		abs, berr := t.resolveAny(s)
		if berr != nil {
			res.appendFailed(s, berr.Message)
			continue
		}
		st, err := os.Stat(abs)
		if err != nil || st.IsDir() {
			res.appendFailed(s, "not a readable file")
			continue
		}
		if st.Size() > cap {
			res.appendFailed(s, sprintf("file is %d bytes, exceeds per-file read limit %d; use read_file with offset/length", st.Size(), cap))
			continue
		}
		if total+st.Size() > cap {
			res.Truncated = true
			break
		}
		data, err := os.ReadFile(abs) // #nosec G304
		if err != nil {
			res.appendFailed(s, "read failed")
			continue
		}
		total += st.Size()
		e := MultiFileEntry{Path: relDisplay(t.cfg.AllowedDirs, abs)}
		if utf8.Valid(data) && !hasBinaryMarker(data) {
			e.Text, e.Encoding = string(data), "utf-8"
		} else {
			e.Encoding = "base64"
			e.Base64 = base64.StdEncoding.EncodeToString(data)
		}
		res.Files = append(res.Files, e)
	}
	for i := range res.Files {
		if res.Files[i].Error != "" {
			res.Failed++
		}
	}
	return res, nil
}

func (r *ReadMultipleResult) appendFailed(path, msg string) {
	r.Files = append(r.Files, MultiFileEntry{Path: path, Error: msg})
}

// ---- edit_file ----

type editOp struct {
	OldText string
	NewText string
}

func (t *tools) editFile(params map[string]any) (*EditResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	rawPath := getString(params, "path", "")
	dryRun := getBool(params, "dryRun", false)
	rawEdits, _ := params["edits"].([]any)
	if len(rawEdits) == 0 {
		return nil, errInvalidParams("edits (array of {oldText,newText}) is required")
	}
	ops := make([]editOp, 0, len(rawEdits))
	for i, re := range rawEdits {
		m, _ := re.(map[string]any)
		if m == nil {
			return nil, errInvalidParams(sprintf("edit %d: must be an object with oldText/newText", i+1))
		}
		oldS, okOld := m["oldText"].(string)
		newS, okNew := m["newText"].(string)
		if !okOld || oldS == "" || !okNew {
			return nil, errInvalidParams(sprintf("edit %d: oldText must be a non-empty string, newText a string ('' to delete)", i+1))
		}
		ops = append(ops, editOp{oldS, newS})
	}

	abs, berr := t.resolveAny(rawPath)
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil || st.IsDir() {
		return nil, errNotFound("file not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
	}
	if st.Size() > maxEditBytesPerFile {
		return nil, errTooLarge(sprintf("file is %d bytes, edit_file is limited to %d bytes", st.Size(), maxEditBytesPerFile))
	}
	data, err := os.ReadFile(abs) // #nosec G304
	if err != nil {
		return nil, errInternal("read failed", err)
	}
	if !utf8.Valid(data) {
		return nil, errInvalidParams("edit_file requires a UTF-8 text file")
	}
	text := string(data)
	totalMatches := 0
	for i, op := range ops {
		n := strings.Count(text, op.OldText)
		if n == 0 {
			return nil, errInvalidParams(sprintf("edit %d: oldText not found (no fuzzy matching applied)", i+1))
		}
		if n > 1 {
			return nil, errInvalidParams(sprintf("edit %d: oldText matches %d locations; expand oldText until it is unique", i+1, n))
		}
		text = strings.Replace(text, op.OldText, op.NewText, 1)
		totalMatches += n
	}
	before := string(data)
	res := &EditResult{
		Path: relDisplay(t.cfg.AllowedDirs, abs), Applied: !dryRun, DryRun: dryRun,
		EditsApplied: len(ops), Matches: totalMatches,
		Diff:       unifiedDiff(before, text),
		SizeBefore: int64(len(before)), SizeAfter: int64(len(text)),
	}
	if dryRun {
		return res, nil
	}
	tmp := abs + ".tmp-localoctop"
	if err := os.WriteFile(tmp, []byte(text), 0o644); err != nil { // #nosec G306 - validated path
		return nil, errInternal("write failed", err)
	}
	if err := os.Rename(tmp, abs); err != nil {
		_ = os.Remove(tmp)
		return nil, errInternal("replace failed", err)
	}
	return res, nil
}

// unifiedDiff renders a git-style unified diff (3 context lines) by trimming
// the common prefix/suffix lines and replacing the middle block. Accurate for
// the targeted-replacement semantics of edit_file without a full LCS.
func unifiedDiff(before, after string) string {
	bLines := strings.Split(strings.ReplaceAll(before, "\r\n", "\n"), "\n")
	aLines := strings.Split(strings.ReplaceAll(after, "\r\n", "\n"), "\n")
	lo := 0
	for lo < len(bLines) && lo < len(aLines) && bLines[lo] == aLines[lo] {
		lo++
	}
	hiB, hiA := len(bLines), len(aLines)
	for hiB > lo && hiA > lo && bLines[hiB-1] == aLines[hiA-1] {
		hiB--
		hiA--
	}
	ctx := 3
	sB, eB := lo, hiB
	sA, eA := lo, hiA
	if sB-ctx > 0 {
		sB -= ctx
	} else {
		sB = 0
	}
	if eB+ctx < len(bLines) {
		eB += ctx
	} else {
		eB = len(bLines)
	}
	if sA-ctx > 0 {
		sA -= ctx
	} else {
		sA = 0
	}
	if eA+ctx < len(aLines) {
		eA += ctx
	} else {
		eA = len(aLines)
	}
	var sb strings.Builder
	sb.WriteString(sprintf("@@ -%d,%d +%d,%d @@\n", sB+1, eB-sB, sA+1, eA-sA))
	for i := sB; i < lo; i++ {
		sb.WriteString(" " + bLines[i] + "\n")
	}
	for i := lo; i < hiB; i++ {
		sb.WriteString("-" + bLines[i] + "\n")
	}
	for i := lo; i < hiA; i++ {
		sb.WriteString("+" + aLines[i] + "\n")
	}
	for i := hiA; i < eA; i++ {
		sb.WriteString(" " + aLines[i] + "\n")
	}
	return sb.String()
}

// ---- list_directory_with_sizes ----

func (t *tools) listDirWithSizes(params map[string]any) (*ListWithSizesResult, *BridgeError) {
	abs, berr := t.resolveAny(defaultRoot(t, getString(params, "path", "")))
	if berr != nil {
		return nil, berr
	}
	entries, err := collectDirEntries(abs, abs, 1, 1, maxListEntries)
	if err != nil {
		return nil, errInternal("read dir failed", err)
	}
	sortBy := getString(params, "sortBy", "name")
	out := &ListWithSizesResult{Path: relDisplay(t.cfg.AllowedDirs, abs), Entries: make([]SizeListEntry, 0, len(entries))}
	for _, e := range entries {
		out.Entries = append(out.Entries, SizeListEntry{Name: e.Name, Type: e.Type, Size: e.Size})
		if e.Type == "dir" {
			out.TotalDirs++
		} else {
			out.TotalFiles++
			out.TotalBytes += e.Size
		}
	}
	if sortBy == "size" {
		sort.SliceStable(out.Entries, func(i, j int) bool {
			if (out.Entries[i].Type == "file") != (out.Entries[j].Type == "file") {
				return out.Entries[i].Type == "file" // files first, then dirs
			}
			return out.Entries[i].Size > out.Entries[j].Size
		})
	} else {
		sort.SliceStable(out.Entries, func(i, j int) bool { return out.Entries[i].Name < out.Entries[j].Name })
	}
	return out, nil
}

func defaultRoot(t *tools, p string) string {
	if p == "" && len(t.cfg.AllowedDirs) > 0 {
		return t.cfg.AllowedDirs[0]
	}
	return p
}

// ---- directory_tree ----

func (t *tools) directoryTree(params map[string]any) (*DirectoryTreeResult, *BridgeError) {
	abs, berr := t.resolveAny(defaultRoot(t, getString(params, "path", "")))
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil || !st.IsDir() {
		return nil, errInvalidParams("path is not a directory")
	}
	excludes := stringSlice(params, "excludePatterns")
	res := &DirectoryTreeResult{Path: relDisplay(t.cfg.AllowedDirs, abs)}
	count := 0
	res.Tree, res.Truncated = buildTree(abs, excludes, &count)
	return res, nil
}

func buildTree(dir string, excludes []string, count *int) ([]*TreeNode, bool) {
	raw, err := os.ReadDir(dir)
	if err != nil {
		return []*TreeNode{}, false
	}
	out := make([]*TreeNode, 0, len(raw))
	for _, e := range raw {
		if *count >= maxTreeNodes {
			return out, true
		}
		name := e.Name()
		if matchAnyName(name, excludes) {
			continue
		}
		*count++
		node := &TreeNode{Name: name}
		if e.IsDir() {
			node.Type = "directory"
			children, trunc := buildTree(filepath.Join(dir, name), excludes, count)
			node.Children = children
			out = append(out, node)
			if trunc {
				return out, true
			}
		} else {
			node.Type = "file"
			out = append(out, node)
		}
	}
	return out, false
}

func matchAnyName(name string, patterns []string) bool {
	for _, p := range patterns {
		if p == "" {
			continue
		}
		if ok, _ := filepath.Match(filepath.FromSlash(p), name); ok {
			return true
		}
		if !isGlob(p) && strings.Contains(strings.ToLower(name), strings.ToLower(p)) {
			return true
		}
	}
	return false
}

// ---- move_file ----

func (t *tools) moveFile(params map[string]any) (*MoveResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	srcRaw := getString(params, "source", "")
	dstRaw := getString(params, "destination", "")
	if srcRaw == "" || dstRaw == "" {
		return nil, errInvalidParams("source and destination are required")
	}
	src, berr := t.resolveAny(srcRaw)
	if berr != nil {
		return nil, berr
	}
	_, dst, berr2 := t.guard.Resolve(dstRaw)
	if berr2 != nil {
		return nil, berr2
	}
	if samePath(src, dst) {
		return nil, errInvalidParams("source and destination are the same path")
	}
	if _, err := os.Lstat(dst); err == nil {
		return nil, errInvalidParams("destination already exists (move_file never overwrites)")
	}
	// Moving a directory into its own subtree would destroy it.
	if st, serr := os.Lstat(src); serr == nil && st.IsDir() {
		if rel, rerr := filepath.Rel(src, dst); rerr == nil && !strings.HasPrefix(rel, "..") {
			return nil, errInvalidParams("cannot move a directory into its own subtree")
		}
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return nil, errInternal("create destination parent failed", err)
	}
	if err := os.Rename(src, dst); err != nil {
		return nil, errInternal("rename failed", err)
	}
	return &MoveResult{
		Source:      relDisplay(t.cfg.AllowedDirs, src),
		Destination: relDisplay(t.cfg.AllowedDirs, dst),
		Moved:       true,
	}, nil
}

// ---- delete_file ----

func (t *tools) deleteFile(params map[string]any) (*DeleteResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	abs, berr := t.resolveAny(getString(params, "path", ""))
	if berr != nil {
		return nil, berr
	}
	st, err := os.Lstat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("path not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("stat failed", err)
	}
	if st.IsDir() {
		return nil, errInvalidParams("path is a directory; use remove_directory")
	}
	permanent := getBool(params, "permanent", false)
	return t.removeOne(abs, false, permanent)
}

// ---- remove_directory ----

func (t *tools) removeDirectory(params map[string]any) (*DeleteResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	abs, berr := t.resolveAny(getString(params, "path", ""))
	if berr != nil {
		return nil, berr
	}
	st, err := os.Lstat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("path not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("stat failed", err)
	}
	if !st.IsDir() {
		return nil, errInvalidParams("path is a file; use delete_file")
	}
	// Never delete a whitelist root itself.
	for _, r := range t.cfg.AllowedDirs {
		if samePath(r, abs) {
			return nil, errInvalidParams("refusing to remove a whitelist root directory")
		}
	}
	recursive := getBool(params, "recursive", false)
	permanent := getBool(params, "permanent", false)
	if !recursive {
		if ents, rerr := os.ReadDir(abs); rerr == nil && len(ents) > 0 {
			return nil, errInvalidParams("directory is not empty; pass recursive=true to remove its contents")
		}
	}
	return t.removeOne(abs, recursive, permanent)
}

// removeOne deletes one path honouring the recycle-bin default. When the
// platform cannot offer a recycle bin we REFUSE rather than silently
// hard-delete (the employee's trash stays available); only permanent=true
// forces the irreversible path.
func (t *tools) removeOne(abs string, recursive, permanent bool) (*DeleteResult, *BridgeError) {
	entries := 1
	if recursive {
		entries = countSubtree(abs)
	}
	rel := relDisplay(t.cfg.AllowedDirs, abs)
	if !permanent {
		ok, err := trashPath(abs)
		if err != nil {
			return nil, errInternal("recycle-bin move failed", err)
		}
		if !ok {
			return nil, errInvalidParams("this platform offers no recycle bin; re-send with permanent=true to confirm irreversible deletion")
		}
		return &DeleteResult{Path: rel, Deleted: true, Recycle: true, Entries: entries}, nil
	}
	var err error
	if recursive {
		err = os.RemoveAll(abs)
	} else {
		err = os.Remove(abs)
	}
	if err != nil {
		return nil, errInternal("delete failed", err)
	}
	return &DeleteResult{Path: rel, Deleted: true, Permanent: true, Entries: entries}, nil
}

func countSubtree(dir string) int {
	n := 0
	_ = filepath.WalkDir(dir, func(_ string, _ fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		n++
		if n > maxWalkNodes {
			return filepath.SkipAll
		}
		return nil
	})
	return n
}

func samePath(a, b string) bool {
	aa, aerr := filepath.Abs(a)
	bb, berr := filepath.Abs(b)
	if aerr != nil || berr != nil {
		return false
	}
	return strings.EqualFold(aa, bb)
}

// ---- zip_files ----

func (t *tools) zipFiles(params map[string]any) (*ZipResult, *BridgeError) {
	// Zipping reads files and returns the bundle — a READ composite (the
	// archive itself lives in the response, not on disk). Read tools stay
	// available under the read switch; no write gate here.
	manifest := stringSlice(params, "paths")
	if len(manifest) == 0 {
		return nil, errInvalidParams("paths (array of whitelist files/directories) is required")
	}
	mediaCap := t.cfg.MaxMediaBytes
	if mediaCap <= 0 {
		mediaCap = maxMediaBytesDefault
	}
	excludes := stringSlice(params, "excludePatterns")
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	count := 0
	truncated := false
	skipped := 0

	addMember := func(absPath, entryName string) *BridgeError {
		if int64(buf.Len()) > mediaCap || count >= maxZipEntries {
			truncated = true
			return nil
		}
		src, err := os.Open(absPath) // #nosec G304 - validated
		if err != nil {
			skipped++
			return nil
		}
		defer src.Close()
		st, serr := src.Stat()
		if serr != nil {
			skipped++
			return nil
		}
		hw := &zip.FileHeader{Name: filepath.ToSlash(entryName), Method: zip.Deflate}
		hw.SetModTime(st.ModTime())
		w, werr := zw.CreateHeader(hw)
		if werr != nil {
			return errInternal("zip create failed", werr)
		}
		if _, cerr := io.Copy(w, src); cerr != nil {
			return errInternal("zip write failed", cerr)
		}
		count++
		return nil
	}

	for _, rp := range manifest {
		if truncated {
			break
		}
		abs, berr := t.resolveAny(rp)
		if berr != nil {
			skipped++
			continue
		}
		rootRel := relDisplay(t.cfg.AllowedDirs, abs)
		st, err := os.Stat(abs)
		if err != nil {
			skipped++
			continue
		}
		if !st.IsDir() {
			if zerr := addMember(abs, rootRel); zerr != nil {
				return nil, zerr
			}
			continue
		}
		werr := filepath.WalkDir(abs, func(p string, d fs.DirEntry, e error) error {
			if e != nil {
				return nil
			}
			if d.IsDir() {
				if n := d.Name(); matchAnyName(n, excludes) || n == ".git" || n == "node_modules" {
					if p != abs {
						return fs.SkipDir
					}
				}
				return nil
			}
			if matchAnyName(d.Name(), excludes) {
				return nil
			}
			rel, rerr := filepath.Rel(abs, p)
			if rerr != nil {
				rel = d.Name()
			}
			if zerr := addMember(p, filepath.Join(rootRel, rel)); zerr != nil {
				return zerr
			}
			if truncated {
				return filepath.SkipAll
			}
			return nil
		})
		if werr != nil {
			if be, ok := werr.(*BridgeError); ok {
				return nil, be
			}
			return nil, errInternal("walk failed", werr)
		}
	}
	if count == 0 {
		return nil, errNotFound("no matching paths to archive", nil)
	}
	if cerr := zw.Close(); cerr != nil {
		return nil, errInternal("zip close failed", cerr)
	}
	archive := buf.Bytes()
	return &ZipResult{
		Base64:    base64.StdEncoding.EncodeToString(archive),
		Size:      int64(len(archive)),
		Entries:   count,
		Skipped:   skipped,
		Truncated: truncated,
	}, nil
}

// ---- unzip_file ----

func (t *tools) unzipFile(params map[string]any) (*UnzipResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	archiveRaw := getString(params, "archive", "")
	destRaw := getString(params, "dest", "")
	overwrite := getBool(params, "overwrite", false)
	absArch, berr := t.resolveAny(archiveRaw)
	if berr != nil {
		return nil, berr
	}
	_, absDest, berr2 := t.guard.Resolve(destRaw)
	if berr2 != nil {
		return nil, berr2
	}
	mediaCap := t.cfg.MaxMediaBytes
	if mediaCap <= 0 {
		mediaCap = maxMediaBytesDefault
	}
	zr, err := zip.OpenReader(absArch) // #nosec G304
	if err != nil {
		return nil, errInvalidParams("not a valid zip archive: " + err.Error())
	}
	defer zr.Close()
	if err := os.MkdirAll(absDest, 0o755); err != nil {
		return nil, errInternal("create dest failed", err)
	}
	res := &UnzipResult{Archive: relDisplay(t.cfg.AllowedDirs, absArch), Dest: relDisplay(t.cfg.AllowedDirs, absDest)}
	for _, f := range zr.File {
		if res.Extracted >= maxZipEntries {
			break
		}
		target, terr := resolveInside(absDest, f.Name)
		if terr != nil {
			return nil, terr
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return nil, errInternal("mkdir failed", err)
			}
			continue
		}
		if res.TotalBytes+int64(f.UncompressedSize64) > mediaCap {
			break // expansion-budget guard (zip-bomb defence)
		}
		if _, statErr := os.Lstat(target); statErr == nil && !overwrite {
			return nil, errInvalidParams("member target already exists: " + filepath.ToSlash(target) + " (pass overwrite=true)")
		}
		rc, oerr := f.Open()
		if oerr != nil {
			return nil, errInternal("open member failed", oerr)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			rc.Close()
			return nil, errInternal("mkdir parent failed", err)
		}
		out, ferr := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644) // #nosec G304 - resolveInside-validated
		if ferr != nil {
			rc.Close()
			return nil, errInternal("create member failed", ferr)
		}
		written, cerr := io.Copy(out, io.LimitReader(rc, mediaCap))
		out.Close()
		rc.Close()
		if cerr != nil {
			return nil, errInternal("extract member failed", cerr)
		}
		_ = os.Chtimes(target, time.Now(), f.Modified)
		res.Extracted++
		res.TotalBytes += written
	}
	return res, nil
}

// resolveInside joins an archive entry to dest and proves the result stays
// within dest, defeating ../ traversal and absolute-path members (zip slip).
func resolveInside(dest, entry string) (string, *BridgeError) {
	e := filepath.FromSlash(strings.TrimPrefix(filepath.ToSlash(entry), "/"))
	if e == "" {
		return "", errInvalidParams("empty archive member name: " + entry)
	}
	target := filepath.Join(dest, e)
	if filepath.Clean(target) != filepath.Clean(dest) &&
		!strings.HasPrefix(filepath.Clean(target)+string(filepath.Separator), filepath.Clean(dest)+string(filepath.Separator)) {
		return "", errNotAllowed("archive member escapes destination: " + entry)
	}
	return target, nil
}

// ---- list_allowed_directories ----

func (t *tools) listAllowedDirectories(_ map[string]any) (*AllowedDirsResult, *BridgeError) {
	out := &AllowedDirsResult{Directories: make([]string, 0, len(t.cfg.AllowedDirs))}
	for _, d := range t.cfg.AllowedDirs {
		out.Directories = append(out.Directories, filepath.ToSlash(d))
	}
	out.Note = "Only these directory subtrees are served; every path outside them is refused (pathguard)."
	return out, nil
}

// ---- search_files v0.5 overlay: mode=regex + excludePatterns ----

// searchFilesV05 extends the v0.4 search with regex mode and exclusions.
// Non-regex requests delegate to the original implementation verbatim.
func (t *tools) searchFilesV05(params map[string]any) (*SearchResult, *BridgeError) {
	pattern := getString(params, "pattern", "")
	if pattern == "" {
		return nil, errInvalidParams("pattern is required")
	}
	mode := getString(params, "mode", "")
	excludes := stringSlice(params, "excludePatterns")

	if mode != "regex" {
		res, berr := t.searchFiles(params)
		if berr != nil {
			return nil, berr
		}
		if len(excludes) > 0 {
			kept := make([]SearchHit, 0, len(res.Matches))
			for _, m := range res.Matches {
				if !matchAnyName(filepath.Base(filepath.FromSlash(m.Path)), excludes) {
					kept = append(kept, m)
				}
			}
			res.Matches = kept
		}
		return res, nil
	}

	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil, errInvalidParams("invalid regex: " + err.Error())
	}
	rawPath := defaultRoot(t, getString(params, "path", ""))
	_, abs, berr := t.guard.Resolve(rawPath)
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil || !st.IsDir() {
		return nil, errInvalidParams("search path must be a directory")
	}
	limit := defaultSearchLimit
	if v := getInt(params, "max_results", -1); v > 0 {
		limit = v
	}
	if v := getInt(params, "limit", -1); v > 0 {
		limit = v
	}
	if limit > maxSearchHits {
		limit = maxSearchHits
	}
	matchContent := getBool(params, "match_content", false)
	includeDirs := getBool(params, "include_dirs", true)
	recursive := getBool(params, "recursive", true)

	res := &SearchResult{Pattern: pattern, Path: relDisplay(t.cfg.AllowedDirs, abs), Matches: []SearchHit{}}
	walked := 0
	werr := filepath.WalkDir(abs, func(p string, d fs.DirEntry, e error) error {
		if e != nil {
			return nil
		}
		walked++
		if walked > maxWalkNodes {
			res.Truncated = true
			return filepath.SkipAll
		}
		if p == abs {
			return nil
		}
		if !recursive {
			if rel, rerr := filepath.Rel(abs, p); rerr == nil && strings.Contains(rel, string(filepath.Separator)) {
				if d.IsDir() {
					return fs.SkipDir
				}
				return nil
			}
		}
		if d.IsDir() {
			n := d.Name()
			if n == ".git" || n == ".svn" || n == "node_modules" || n == "__pycache__" || matchAnyName(n, excludes) {
				return fs.SkipDir
			}
			if !includeDirs {
				return nil
			}
		} else if matchAnyName(d.Name(), excludes) {
			return nil
		}
		rel, _ := filepath.Rel(abs, p)
		nameHit := re.MatchString(d.Name()) || re.MatchString(filepath.ToSlash(rel))
		hit := SearchHit{Path: relDisplay(t.cfg.AllowedDirs, p)}
		contentHit := false
		if !d.IsDir() && matchContent {
			ln, preview, ok := regexFirstLine(p, re, t.cfg.MaxReadBytes)
			contentHit = ok
			hit.Line = ln
			hit.Preview = preview
		}
		if !nameHit && !contentHit {
			return nil
		}
		if info, ierr := d.Info(); ierr == nil {
			hit.Size = info.Size()
			hit.MTime = info.ModTime().UTC().Format(time.RFC3339)
		}
		res.Matches = append(res.Matches, hit)
		if len(res.Matches) >= limit {
			res.Truncated = true
			return filepath.SkipAll
		}
		return nil
	})
	if werr != nil {
		return nil, errInternal("walk failed", werr)
	}
	return res, nil
}

// regexFirstLine reports the first line of a text file matching re, with a
// capped preview.
func regexFirstLine(path string, re *regexp.Regexp, maxBytes int64) (int, string, bool) {
	st, err := os.Stat(path)
	if err != nil || st.IsDir() || st.Size() > maxBytes {
		return 0, "", false
	}
	data, err := os.ReadFile(path) // #nosec G304 - inside validated walk root
	if err != nil || !utf8.Valid(data) {
		return 0, "", false
	}
	for i, line := range strings.Split(string(data), "\n") {
		if re.MatchString(line) {
			prev := line
			if len(prev) > 200 {
				prev = prev[:200]
			}
			return i + 1, prev, true
		}
	}
	return 0, "", false
}

// stringSlice coerces a JSON array param to []string, dropping non-strings.
func stringSlice(params map[string]any, key string) []string {
	raw, _ := params[key].([]any)
	out := make([]string, 0, len(raw))
	for _, v := range raw {
		if s, ok := v.(string); ok && s != "" {
			out = append(out, s)
		}
	}
	return out
}
