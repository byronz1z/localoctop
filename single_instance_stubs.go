//go:build !windows

package localoctop

// ShowAlreadyRunningMessageBox on non-Windows is a no-op: the desktop app is
// Windows-only in practice, and a background bridge's stderr line is the
// right answer everywhere else. Defined so desktop's main.go compiles on
// every platform go vet runs on.
func ShowAlreadyRunningMessageBox() {}

// BringWailsSingleInstanceWindowToFront on non-Windows has no Win32 window to
// raise; it reports failure so the caller falls back to the stderr exit.
func BringWailsSingleInstanceWindowToFront() bool { return false }
