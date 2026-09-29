//go:build !windows

package localoctop

// Non-Windows stubs: the bridge EXE targets Windows employee machines, but
// the package must stay cross-compilable for CI and tests. On Linux/macOS we
// report "no recycle bin" so removeOne refuses unless the caller explicitly
// passes permanent=true.

func trashPath(abs string) (movedToRecycle bool, err error) {
	return false, nil
}
