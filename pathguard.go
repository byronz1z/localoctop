package octobridge

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
)

// pathGuard resolves and validates caller-supplied paths against the
// configured whitelist. It is the single chokepoint every tool handler must
// pass through; there is no other way to touch the filesystem.
//
// It defends against, in order:
//  1. Absolute paths outside any allowed root.
//  2. Lexical traversal ("..", embedded separators, NUL bytes).
//  3. Symlink escape: every path is fully resolved with EvalSymlinks and the
//     *resolved* target must still sit inside an allowed root.
//  4. Windows case-insensitivity and drive-letter quirks.
type pathGuard struct {
	roots []string // absolute, cleaned, with trailing separator semantics handled in isInside
	log   Logger
}

func newPathGuard(roots []string, log Logger) *pathGuard {
	if log == nil {
		log = NopLogger{}
	}
	return &pathGuard{roots: roots, log: log}
}

// resolveRelative maps a whitelist-relative path to an absolute one under
// root, then verifies the resolved real path is still inside root.
//
//   - relPath is interpreted relative to root. It must be a clean relative
//     path; any leading separator, drive letter, or ".." component is rejected.
//   - The returned absolute path has symlinks resolved (EvalSymlinks) for
//     existing segments, and is guaranteed to remain within root.
func (g *pathGuard) resolveRelative(root, relPath string) (string, *BridgeError) {
	root = filepath.Clean(root)
	if relPath == "" || relPath == "." {
		// The root itself.
		return g.ensureInside(root, root)
	}
	if strings.ContainsRune(relPath, 0) {
		return "", errNotAllowed("path contains NUL byte")
	}
	// Reject absolute paths and drive-qualified paths in the relative form.
	if filepath.IsAbs(relPath) {
		return "", errNotAllowed("relative path must not be absolute")
	}
	if hasWindowsDrive(relPath) {
		return "", errNotAllowed("relative path must not contain a drive letter")
	}
	// Reject UNC forms (\\host\share).
	if strings.HasPrefix(relPath, `\\`) || strings.HasPrefix(relPath, "//") {
		return "", errNotAllowed("relative path must not be a UNC path")
	}

	// Normalize to forward-slash, clean, and inspect components for traversal.
	norm := filepath.ToSlash(relPath)
	cleaned := path.Clean(norm) // path (not filepath) so the check is portable
	if cleaned == ".." || strings.HasPrefix(cleaned, "../") {
		return "", errNotAllowed(`path traversal ("..") is not allowed`)
	}
	for _, seg := range strings.Split(cleaned, "/") {
		if seg == ".." {
			return "", errNotAllowed(`path traversal ("..") is not allowed`)
		}
	}

	candidate := filepath.Join(root, filepath.FromSlash(cleaned))
	return g.ensureInside(root, candidate)
}

// resolveAbsolute validates a caller-supplied absolute path. It must resolve
// (after symlink evaluation) to a location inside one of the roots.
func (g *pathGuard) resolveAbsolute(absPath string) (root, resolved string, err *BridgeError) {
	if strings.ContainsRune(absPath, 0) {
		return "", "", errNotAllowed("path contains NUL byte")
	}
	if !filepath.IsAbs(absPath) && !hasWindowsDrive(absPath) {
		return "", "", errInvalidParams("path must be absolute, or use resolveRelative")
	}
	clean := filepath.Clean(absPath)

	// Pass 1: compare fully-resolved forms. This catches Windows 8.3
	// short-name vs long-name mismatches (C:\Users\BYRON~1 vs C:\Users\Byron)
	// and symlinks in either the root or the candidate.
	if realCandidate, cerr := evalSymlinksBestEffort(clean); cerr == nil {
		for _, r := range g.roots {
			rootReal := r
			if rr, e := filepath.EvalSymlinks(r); e == nil {
				rootReal = filepath.Clean(rr)
			}
			if _, ok := g.isInside(rootReal, realCandidate); ok {
				return r, realCandidate, nil
			}
		}
	}

	// Pass 2: lexical containment for paths that do not exist yet (write
	// targets). ensureInside still re-verifies after resolution.
	for _, r := range g.roots {
		if _, ok := g.isInside(r, clean); ok {
			res, rerr := g.ensureInside(r, clean)
			if rerr != nil {
				return "", "", rerr
			}
			return r, res, nil
		}
	}
	return "", "", errNotAllowed("path is outside the whitelist")
}

// Resolve takes any caller path. If it is absolute it is validated against the
// whitelist directly; if relative it is resolved under the first root (the
// conventional default). Returns the chosen root and the safe absolute path.
func (g *pathGuard) Resolve(p string) (root, abs string, err *BridgeError) {
	if p == "" {
		return "", "", errInvalidParams("path is required")
	}
	if filepath.IsAbs(p) || hasWindowsDrive(p) {
		return g.resolveAbsolute(p)
	}
	if len(g.roots) == 0 {
		return "", "", errNotAllowed("no whitelist roots configured")
	}
	root = g.roots[0]
	abs, err = g.resolveRelative(root, p)
	if err != nil {
		return "", "", err
	}
	return root, abs, nil
}

// ensureInside fully resolves candidate (symlinks + "..") and confirms the
// real path is inside root. This is the symlink-escape defense.
func (g *pathGuard) ensureInside(root, candidate string) (string, *BridgeError) {
	rootReal := root
	if rr, err := filepath.EvalSymlinks(root); err == nil {
		rootReal = filepath.Clean(rr)
	}
	// EvalSymlinks fails if the path does not exist. For write/create targets
	// we evaluate the deepest existing ancestor and re-append the remainder.
	realCandidate, err := evalSymlinksBestEffort(candidate)
	if err != nil {
		return "", errNotFound("path not found", err)
	}
	if _, ok := g.isInside(rootReal, realCandidate); !ok {
		g.log.Warnf("blocked symlink/traversal escape: %s -> %s (root %s)", candidate, realCandidate, rootReal)
		return "", errNotAllowed("resolved path escapes the whitelist (symlink or traversal)")
	}
	return realCandidate, nil
}

// isInside reports whether child is root itself or located under root,
// comparing case-insensitively on Windows and using the platform separator.
func (g *pathGuard) isInside(root, child string) (string, bool) {
	root = filepath.Clean(root)
	child = filepath.Clean(child)
	if eqPath(root, child) {
		return root, true
	}
	prefix := root
	if !strings.HasSuffix(prefix, string(filepath.Separator)) {
		prefix += string(filepath.Separator)
	}
	if hasPrefixPath(child, prefix) {
		return root, true
	}
	return "", false
}

// evalSymlinksBestEffort resolves symlinks for the deepest existing ancestor
// of p and appends the unresolved remainder. It returns an error only if no
// ancestor exists at all.
func evalSymlinksBestEffort(p string) (string, error) {
	if resolved, err := filepath.EvalSymlinks(p); err == nil {
		return filepath.Clean(resolved), nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		// Permission or I/O error: surface it.
		return "", err
	}
	// Walk up to the deepest existing ancestor.
	cur := filepath.Clean(p)
	var tail []string
	for {
		parent := filepath.Dir(cur)
		if parent == cur {
			// Reached filesystem root without finding an existing ancestor.
			return "", &os.PathError{Op: "evalsymlinks", Path: p, Err: fs.ErrNotExist}
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		if resolved, err := filepath.EvalSymlinks(parent); err == nil {
			parts := append([]string{resolved}, tail...)
			return filepath.Clean(filepath.Join(parts...)), nil
		}
		cur = parent
	}
}

// hasWindowsDrive reports a leading "C:" style drive qualifier.
func hasWindowsDrive(p string) bool {
	if len(p) < 2 {
		return false
	}
	c := p[0]
	isLetter := (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
	return isLetter && p[1] == ':'
}

func eqPath(a, b string) bool {
	if pathCaseInsensitive() {
		return strings.EqualFold(a, b)
	}
	return a == b
}

func hasPrefixPath(s, prefix string) bool {
	if pathCaseInsensitive() {
		return len(s) >= len(prefix) && strings.EqualFold(s[:len(prefix)], prefix)
	}
	return strings.HasPrefix(s, prefix)
}

// pathCaseInsensitive reports whether the current platform compares paths
// case-insensitively (Windows and macOS both do).
func pathCaseInsensitive() bool {
	return runtime.GOOS == "windows" || runtime.GOOS == "darwin"
}

// describeRoots renders the whitelist for log/audit context.
func (g *pathGuard) describeRoots() string {
	return fmt.Sprintf("[%s]", strings.Join(g.roots, ", "))
}
