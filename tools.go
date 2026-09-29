package localoctop

import (
	"encoding/base64"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

// tools implements the four read-only tools plus the two reserved write tools.
// Every method resolves its path through the guard first; nothing reaches the
// filesystem otherwise.
type tools struct {
	cfg   Config
	guard *pathGuard
	log   Logger
}

func newTools(cfg Config, guard *pathGuard, log Logger) *tools {
	return &tools{cfg: cfg, guard: guard, log: log}
}

// maxListEntries bounds directory listings and search results so a huge tree
// cannot exhaust memory or blow the WebSocket frame limit.
const (
	maxListEntries = 5000
	maxSearchHits  = 1000
	maxWalkNodes   = 200000

	// Tool-contract defaults per task spec v1.1 §7.2: list_directory limit
	// (default 500, max 2000), depth (default 1, max 3); search_files limit
	// (default 200). Hard caps above remain as the transport guard.
	defaultListLimit   = 500
	maxListLimit       = 2000
	defaultListDepth   = 1
	maxListDepth       = 3
	defaultSearchLimit = 200
)

// getString pulls a string param, applying a default when absent/empty.
func getString(params map[string]any, key, def string) string {
	if params == nil {
		return def
	}
	if v, ok := params[key]; ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return def
}

// getInt pulls an integer param (JSON numbers decode to float64).
func getInt(params map[string]any, key string, def int) int {
	return toInt(params[key], def)
}

// toInt coerces a decoded-JSON value to int; returns def when absent/wrong type.
func toInt(v any, def int) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	}
	return def
}

// getBool pulls a boolean param.
func getBool(params map[string]any, key string, def bool) bool {
	if params == nil {
		return def
	}
	if v, ok := params[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return def
}

// ---- list_directory ----

func (t *tools) listDirectory(params map[string]any) (*ListDirectoryResult, *BridgeError) {
	rawPath := getString(params, "path", "")
	if rawPath == "" {
		// Default to the first whitelist root when no path given.
		if len(t.cfg.AllowedDirs) == 0 {
			return nil, errInvalidParams("path is required")
		}
		rawPath = t.cfg.AllowedDirs[0]
	}
	root, abs, berr := t.guard.Resolve(rawPath)
	if berr != nil {
		return nil, berr
	}
	_ = root

	st, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("directory not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("stat failed", err)
	}
	if !st.IsDir() {
		return nil, errInvalidParams("path is not a directory")
	}

	// depth: 1..3, default 1 (§7.2). limit: 1..2000, default 500 (§7.2);
	// the hard cap maxListEntries stays as a defensive backstop.
	depth := getInt(params, "depth", defaultListDepth)
	if depth < 1 {
		depth = 1
	}
	if depth > maxListDepth {
		depth = maxListDepth
	}
	limit := getInt(params, "limit", defaultListLimit)
	if limit < 1 || limit > maxListLimit {
		limit = maxListLimit
	}

	entries, err := collectDirEntries(abs, abs, 1, depth, limit)
	if err != nil {
		return nil, errInternal("read dir failed", err)
	}
	return &ListDirectoryResult{Path: relDisplay(t.cfg.AllowedDirs, abs), Entries: entries}, nil
}

// collectDirEntries walks from dir up to maxDepth levels below root (inclusive,
// 1 = immediate children only). Entry names are paths relative to dir so that
// nested results stay unambiguous. limit is a hard cap across all levels;
// hitting it stops the walk (deeper siblings beyond maxWalkNodes are skipped).
func collectDirEntries(root, dir string, level, maxDepth, limit int) ([]DirEntry, error) {
	out := make([]DirEntry, 0, limit)
	var walk func(d string, lvl int) error
	walk = func(d string, lvl int) error {
		ents, err := os.ReadDir(d)
		if err != nil {
			if lvl == 1 {
				return err
			}
			return nil // unreadable nested dir: skip, don't abort
		}
		for _, e := range ents {
			if len(out) >= limit {
				return nil
			}
			p := filepath.Join(d, e.Name())
			info, ierr := e.Info()
			var size int64
			var mt time.Time
			if ierr == nil {
				size = info.Size()
				mt = info.ModTime()
			}
			typ := "file"
			if e.IsDir() {
				typ = "dir"
			} else if e.Type()&fs.ModeSymlink != 0 {
				typ = "symlink"
			}
			name, rerr := filepath.Rel(root, p)
			if rerr != nil {
				name = e.Name()
			}
			out = append(out, DirEntry{
				Name:  name,
				Type:  typ,
				Size:  size,
				MTime: mt.UTC().Format(time.RFC3339),
			})
			// Recurse only into real dirs (never follow symlinks) and only
			// while levels remain.
			if e.IsDir() && lvl < maxDepth {
				if err := walk(p, lvl+1); err != nil {
					return err
				}
			}
		}
		return nil
	}
	if err := walk(dir, 1); err != nil {
		return nil, err
	}
	return out, nil
}

// ---- read_file ----

func (t *tools) readFile(params map[string]any) (*ReadFileResult, *BridgeError) {
	rawPath := getString(params, "path", "")
	if rawPath == "" {
		return nil, errInvalidParams("path is required")
	}
	_, abs, berr := t.guard.Resolve(rawPath)
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
		return nil, errInvalidParams("path is a directory; use list_directory")
	}

	// offset/length per §7.2: byte-window read, total returned ≤ MaxReadBytes.
	offset := getInt(params, "offset", 0)
	if offset < 0 {
		offset = 0
	}
	if int64(offset) > st.Size() {
		offset = int(st.Size())
	}
	// length default = whole file from offset; caller-supplied length is
	// honoured but capped at MaxReadBytes so a single read stays bounded.
	length := getInt(params, "length", 0)
	want := st.Size() - int64(offset)
	if length > 0 && int64(length) < want {
		want = int64(length)
	}
	if want > t.cfg.MaxReadBytes {
		want = t.cfg.MaxReadBytes
	}
	// Reading a window is always within the per-file cap (want ≤ MaxReadBytes),
	// but a whole-file read of an oversized file is rejected as before.
	if length <= 0 && st.Size() > t.cfg.MaxReadBytes {
		return nil, errTooLarge(sprintf("file is %d bytes, exceeds read limit %d", st.Size(), t.cfg.MaxReadBytes))
	}

	f, oerr := os.Open(abs) // #nosec G304 - path is validated by guard
	if oerr != nil {
		return nil, errInternal("read failed", oerr)
	}
	defer f.Close()
	if _, serr := f.Seek(int64(offset), io.SeekStart); serr != nil {
		return nil, errInternal("seek failed", serr)
	}
	data, err := io.ReadAll(io.LimitReader(f, want))
	if err != nil {
		return nil, errInternal("read failed", err)
	}
	truncated := int64(len(data)) < st.Size()-int64(offset)

	res := &ReadFileResult{
		Path:      relDisplay(t.cfg.AllowedDirs, abs),
		Size:      st.Size(),
		Offset:    int64(offset),
		Bytes:     int64(len(data)),
		Encoding:  "utf-8",
		Truncated: truncated,
	}
	if utf8.Valid(data) && !hasBinaryMarker(data) {
		res.Text = string(data)
	} else {
		res.Encoding = "base64"
		res.Base64 = base64.StdEncoding.EncodeToString(data)
	}
	return res, nil
}

// hasBinaryMarker reports a NUL byte in the first chunk, a cheap binary tell.
func hasBinaryMarker(data []byte) bool {
	n := len(data)
	if n > 8192 {
		n = 8192
	}
	for i := 0; i < n; i++ {
		if data[i] == 0 {
			return true
		}
	}
	return false
}

// ---- search_files ----

func (t *tools) searchFiles(params map[string]any) (*SearchResult, *BridgeError) {
	pattern := getString(params, "pattern", "")
	if pattern == "" {
		return nil, errInvalidParams("pattern is required")
	}
	rawPath := getString(params, "path", "")
	if rawPath == "" {
		if len(t.cfg.AllowedDirs) == 0 {
			return nil, errInvalidParams("path is required")
		}
		rawPath = t.cfg.AllowedDirs[0]
	}
	// §7.2 names the cap "limit" (default 200); legacy clients send
	// "max_results". Accept both; hard cap stays maxSearchHits.
	maxResults := defaultSearchLimit
	if v, ok := params["limit"]; ok && toInt(v, -1) > 0 {
		maxResults = toInt(v, defaultSearchLimit)
	} else if v := getInt(params, "max_results", -1); v > 0 {
		maxResults = v
	}
	if maxResults > maxSearchHits {
		maxResults = maxSearchHits
	}
	matchContent := getBool(params, "match_content", false)

	_, abs, berr := t.guard.Resolve(rawPath)
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("search root not found", err)
		}
		return nil, errInternal("stat failed", err)
	}
	if !st.IsDir() {
		return nil, errInvalidParams("search path must be a directory")
	}

	res := &SearchResult{Pattern: pattern, Path: relDisplay(t.cfg.AllowedDirs, abs), Matches: []SearchHit{}}
	truncated := false
	walked := 0
	recursive := getBool(params, "recursive", true)
	skipDirs := getBool(params, "skip_vcs_dirs", true)
	// filepath.Match uses the platform separator; normalize pattern to OS form.
	osPattern := filepath.FromSlash(pattern)

	werr := filepath.WalkDir(abs, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			// Skip unreadable entries rather than aborting the whole walk.
			return nil
		}
		walked++
		if walked > maxWalkNodes {
			truncated = true
			return fs.SkipAll
		}
		if p == abs {
			return nil
		}
		// §7.5/§7.2: prune .git / node_modules subtrees (VCS noise, huge trees).
		if d.IsDir() && skipDirs {
			if n := d.Name(); n == ".git" || n == ".svn" || n == "node_modules" || n == "__pycache__" {
				return fs.SkipDir
			}
		}
		// Non-recursive: only immediate children of the search root.
		if !recursive {
			rel0, rerr0 := filepath.Rel(abs, p)
			if rerr0 == nil && strings.Contains(rel0, string(filepath.Separator)) {
				if d.IsDir() {
					return fs.SkipDir
				}
				return nil
			}
		}
		rel, rerr := filepath.Rel(abs, p)
		if rerr != nil {
			rel = d.Name()
		}
		matched := false
		// Match against the base name and the relative path.
		if ok, _ := filepath.Match(osPattern, d.Name()); ok {
			matched = true
		} else if ok, _ := filepath.Match(osPattern, rel); ok {
			matched = true
		} else if strings.Contains(strings.ToLower(d.Name()), strings.ToLower(pattern)) && !isGlob(pattern) {
			// Convenience: plain substring match when pattern is not a glob.
			matched = true
		}
		if !matched {
			return nil
		}
		if d.IsDir() && !getBool(params, "include_dirs", true) {
			return nil
		}
		if matchContent && !d.IsDir() {
			if !fileContains(p, pattern, t.cfg.MaxReadBytes) {
				return nil
			}
		}
		info, ierr := d.Info()
		var size int64
		var mt time.Time
		if ierr == nil {
			size = info.Size()
			mt = info.ModTime()
		}
		res.Matches = append(res.Matches, SearchHit{
			Path:  relDisplay(t.cfg.AllowedDirs, p),
			Size:  size,
			MTime: mt.UTC().Format(time.RFC3339),
		})
		if len(res.Matches) >= maxResults {
			truncated = true
			return fs.SkipAll
		}
		return nil
	})
	if werr != nil {
		return nil, errInternal("walk failed", werr)
	}
	res.Truncated = truncated
	return res, nil
}

// isGlob reports whether the pattern contains wildcard metacharacters.
func isGlob(p string) bool {
	return strings.ContainsAny(p, "*?[]")
}

// fileContains reports whether a file's text contains needle (case-insensitive).
func fileContains(path, needle string, maxBytes int64) bool {
	st, err := os.Stat(path)
	if err != nil || st.IsDir() || st.Size() > maxBytes {
		return false
	}
	data, err := os.ReadFile(path) // #nosec G304 - inside validated walk root
	if err != nil || !utf8.Valid(data) {
		return false
	}
	return strings.Contains(strings.ToLower(string(data)), strings.ToLower(needle))
}

// ---- get_file_info ----

func (t *tools) getFileInfo(params map[string]any) (*FileInfo, *BridgeError) {
	rawPath := getString(params, "path", "")
	if rawPath == "" {
		return nil, errInvalidParams("path is required")
	}
	_, abs, berr := t.guard.Resolve(rawPath)
	if berr != nil {
		return nil, berr
	}
	// Lstat so we can report symlink-ness before following.
	lst, err := os.Lstat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("path not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("lstat failed", err)
	}
	isLink := lst.Mode()&fs.ModeSymlink != 0
	st := lst
	if isLink {
		if resolved, rerr := os.Stat(abs); rerr == nil {
			st = resolved
		}
	}
	typ := "file"
	if st.IsDir() {
		typ = "dir"
	}
	return &FileInfo{
		Path:      relDisplay(t.cfg.AllowedDirs, abs),
		Name:      st.Name(),
		Type:      typ,
		Size:      st.Size(),
		MTime:     st.ModTime().UTC().Format(time.RFC3339),
		ModTimeU:  st.ModTime().Unix(),
		ReadOnly:  st.Mode().Perm()&0o200 == 0,
		Symlink:   isLink,
		MIMEGuess: guessMIME(st.Name(), st.IsDir()),
	}, nil
}

// ---- write_file (reserved) ----

func (t *tools) writeFile(params map[string]any) (*WriteResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	rawPath := getString(params, "path", "")
	if rawPath == "" {
		return nil, errInvalidParams("path is required")
	}
	content := getString(params, "content", "")
	encoding := getString(params, "encoding", "utf-8")

	var payload []byte
	if strings.EqualFold(encoding, "base64") {
		dec, err := base64.StdEncoding.DecodeString(content)
		if err != nil {
			return nil, errInvalidParams("invalid base64 content")
		}
		payload = dec
	} else {
		payload = []byte(content)
	}
	if int64(len(payload)) > t.cfg.MaxWriteBytes {
		return nil, errTooLarge(sprintf("payload is %d bytes, exceeds write limit %d", len(payload), t.cfg.MaxWriteBytes))
	}
	_, abs, berr := t.guard.Resolve(rawPath)
	if berr != nil {
		return nil, berr
	}
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return nil, errInternal("create parent dir failed", err)
	}
	existed := fileExists(abs)
	if err := os.WriteFile(abs, payload, 0o644); err != nil { // #nosec G306 - validated path
		return nil, errInternal("write failed", err)
	}
	return &WriteResult{Path: relDisplay(t.cfg.AllowedDirs, abs), Size: int64(len(payload)), Created: !existed}, nil
}

// ---- create_directory (reserved) ----

func (t *tools) createDirectory(params map[string]any) (*WriteResult, *BridgeError) {
	if !t.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	rawPath := getString(params, "path", "")
	if rawPath == "" {
		return nil, errInvalidParams("path is required")
	}
	_, abs, berr := t.guard.Resolve(rawPath)
	if berr != nil {
		return nil, berr
	}
	existed := fileExists(abs)
	if err := os.MkdirAll(abs, 0o755); err != nil {
		return nil, errInternal("mkdir failed", err)
	}
	return &WriteResult{Path: relDisplay(t.cfg.AllowedDirs, abs), Created: !existed}, nil
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// relDisplay renders an absolute path relative to its whitelist root when
// possible, so audit logs and MCP output show portable, root-relative paths.
func relDisplay(roots []string, abs string) string {
	for _, r := range roots {
		if rel, err := filepath.Rel(r, abs); err == nil && !strings.HasPrefix(rel, "..") {
			return filepath.ToSlash(rel)
		}
	}
	return filepath.ToSlash(abs)
}

// guessMIME returns a best-effort content type from the file extension.
func guessMIME(name string, isDir bool) string {
	if isDir {
		return "inode/directory"
	}
	switch strings.ToLower(filepath.Ext(name)) {
	case ".txt", ".log", ".md", "":
		return "text/plain"
	case ".json":
		return "application/json"
	case ".csv":
		return "text/csv"
	case ".html", ".htm":
		return "text/html"
	case ".xml":
		return "application/xml"
	case ".yaml", ".yml":
		return "application/yaml"
	case ".png":
		return "image/png"
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".gif":
		return "image/gif"
	case ".pdf":
		return "application/pdf"
	case ".zip":
		return "application/zip"
	default:
		return "application/octet-stream"
	}
}
