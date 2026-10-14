//go:build !windows

package localoctop

// Non-Windows stubs: the bridge EXE targets Windows employee machines, but
// the package must stay cross-compilable for CI and tests. On Linux/macOS we
// report "no recycle bin", so every tool that would replace or remove an
// existing item refuses the operation and leaves it in place — there is no
// permanent-delete fallback. Tests substitute a fake bin via trashImpl to
// exercise the "recycled successfully" path.

func trashPath(abs string) (movedToRecycle bool, err error) {
	return false, nil
}
