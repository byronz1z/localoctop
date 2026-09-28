package octobridge

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// newTestGuard builds a guard rooted at a temp dir containing sample files.
func newTestGuard(t *testing.T) (*pathGuard, string) {
	t.Helper()
	root := t.TempDir()
	must(t, os.WriteFile(filepath.Join(root, "hello.txt"), []byte("hi there\n"), 0o644))
	must(t, os.MkdirAll(filepath.Join(root, "sub"), 0o755))
	must(t, os.WriteFile(filepath.Join(root, "sub", "nested.md"), []byte("# nested\n"), 0o644))
	g := newPathGuard([]string{root}, NopLogger{})
	return g, root
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("setup error: %v", err)
	}
}

func TestResolveRelative_OK(t *testing.T) {
	g, root := newTestGuard(t)
	abs, err := g.resolveRelative(root, "hello.txt")
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if filepath.Base(abs) != "hello.txt" {
		t.Fatalf("unexpected resolved path %s", abs)
	}
}

func TestResolveRelative_Nested(t *testing.T) {
	g, root := newTestGuard(t)
	abs, err := g.resolveRelative(root, "sub/nested.md")
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if !strings.HasSuffix(filepath.ToSlash(abs), "sub/nested.md") {
		t.Fatalf("unexpected resolved path %s", abs)
	}
}

// TestResolveRelative_RejectsDotDot is the headline path-traversal defense.
func TestResolveRelative_RejectsDotDot(t *testing.T) {
	g, root := newTestGuard(t)
	cases := []string{
		"../secret.txt",
		"sub/../../secret.txt",
		"..",
		"./../outside",
		"sub/../../../etc/passwd",
	}
	for _, c := range cases {
		if _, err := g.resolveRelative(root, c); err == nil {
			t.Fatalf("expected %q to be rejected, but it was allowed", c)
		} else {
			var be *BridgeError
			if !asBridgeError(err, &be) || be.Code != CodeNotAllowed {
				t.Fatalf("expected CodeNotAllowed for %q, got %v", c, err)
			}
		}
	}
}

// TestResolveAbsolute_RejectsOutside verifies absolute paths outside the root
// are denied even when they exist on disk.
func TestResolveAbsolute_RejectsOutside(t *testing.T) {
	g, _ := newTestGuard(t)
	outside := t.TempDir() // a second, unrelated dir
	target := filepath.Join(outside, "secret.txt")
	must(t, os.WriteFile(target, []byte("top secret"), 0o644))

	if _, _, err := g.resolveAbsolute(target); err == nil {
		t.Fatalf("expected absolute path outside whitelist to be rejected")
	} else {
		var be *BridgeError
		if !asBridgeError(err, &be) || be.Code != CodeNotAllowed {
			t.Fatalf("expected CodeNotAllowed, got %v", err)
		}
	}
}

// TestResolveAbsolute_RejectsTraversalInAbsolute checks ".." inside an
// absolute path that lexically lands outside the root.
func TestResolveAbsolute_RejectsTraversalInAbsolute(t *testing.T) {
	g, root := newTestGuard(t)
	sneaky := filepath.Join(root, "sub", "..", "..", "escape.txt")
	// Lexically this is <parent-of-root>/escape.txt — outside the whitelist.
	if _, _, err := g.resolveAbsolute(sneaky); err == nil {
		t.Fatalf("expected traversal in absolute path to be rejected")
	}
}

// TestResolveAbsolute_AllowsInsideRoot confirms a legitimate absolute path
// under the root is accepted.
func TestResolveAbsolute_AllowsInsideRoot(t *testing.T) {
	g, root := newTestGuard(t)
	target := filepath.Join(root, "hello.txt")
	r, abs, err := g.resolveAbsolute(target)
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if r != root {
		t.Fatalf("expected root %s, got %s", root, r)
	}
	if abs == "" {
		t.Fatalf("empty resolved path")
	}
}

func TestResolveRelative_RejectsAbsolute(t *testing.T) {
	g, root := newTestGuard(t)
	var abs string
	if runtime.GOOS == "windows" {
		abs = `C:\Windows\system32\drivers\etc\hosts`
	} else {
		abs = "/etc/passwd"
	}
	if _, err := g.resolveRelative(root, abs); err == nil {
		t.Fatalf("expected absolute path passed as relative to be rejected")
	}
}

func TestResolveRelative_RejectsNUL(t *testing.T) {
	g, root := newTestGuard(t)
	if _, err := g.resolveRelative(root, "hello.txt\x00.png"); err == nil {
		t.Fatalf("expected NUL byte path to be rejected")
	}
}

// TestResolveSymlinkEscape verifies the symlink defense: a link inside the
// root pointing outside must resolve to the *real* outside target and be
// rejected. Skipped on platforms/filesystems without symlink support.
func TestResolveSymlinkEscape(t *testing.T) {
	g, root := newTestGuard(t)
	outside := t.TempDir()
	secret := filepath.Join(outside, "secret.txt")
	must(t, os.WriteFile(secret, []byte("outside world"), 0o644))

	link := filepath.Join(root, "escape-link")
	if err := os.Symlink(outside, link); err != nil {
		t.Skipf("symlinks unsupported here: %v", err)
	}
	// Resolving escape-link/secret.txt must fail: real path is outside root.
	if _, err := g.resolveRelative(root, filepath.Join("escape-link", "secret.txt")); err == nil {
		t.Fatalf("expected symlink escape to be rejected")
	} else {
		var be *BridgeError
		if !asBridgeError(err, &be) || be.Code != CodeNotAllowed {
			t.Fatalf("expected CodeNotAllowed, got %v", err)
		}
	}
}

// TestResolveSymlinkInsideAllowed verifies a symlink that stays inside the
// root is permitted (we only block escapes, not all links).
func TestResolveSymlinkInsideAllowed(t *testing.T) {
	g, root := newTestGuard(t)
	link := filepath.Join(root, "sub-link")
	if err := os.Symlink(filepath.Join(root, "sub"), link); err != nil {
		t.Skipf("symlinks unsupported here: %v", err)
	}
	if _, err := g.resolveRelative(root, filepath.Join("sub-link", "nested.md")); err != nil {
		t.Fatalf("expected in-root symlink to be allowed, got %v", err)
	}
}

func TestGuardResolve_RoutesAbsoluteVsRelative(t *testing.T) {
	g, root := newTestGuard(t)
	// Relative -> resolved under first root.
	r, abs, err := g.Resolve("hello.txt")
	if err != nil || r != root || filepath.Base(abs) != "hello.txt" {
		t.Fatalf("relative resolve failed: r=%s abs=%s err=%v", r, abs, err)
	}
	// Absolute inside root -> allowed.
	if _, _, err := g.Resolve(filepath.Join(root, "hello.txt")); err != nil {
		t.Fatalf("absolute inside root should resolve: %v", err)
	}
}

// asBridgeError is a local errors.As helper to keep test imports tidy.
func asBridgeError(err error, target **BridgeError) bool {
	if be, ok := err.(*BridgeError); ok {
		*target = be
		return true
	}
	return false
}

func TestIsInside(t *testing.T) {
	g, root := newTestGuard(t)
	if _, ok := g.isInside(root, root); !ok {
		t.Fatal("root should be inside itself")
	}
	if _, ok := g.isInside(root, filepath.Join(root, "a", "b")); !ok {
		t.Fatal("child should be inside root")
	}
	// A sibling that merely shares a prefix string must NOT match.
	sibling := root + "-evil"
	if _, ok := g.isInside(root, filepath.Join(sibling, "x")); ok {
		t.Fatal("prefix-sibling directory must not be treated as inside root")
	}
}
