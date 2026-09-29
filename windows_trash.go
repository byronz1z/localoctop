//go:build windows

package localoctop

// Windows recycle-bin support via the shell operation API. FOF_ALLOWUNDO
// asks the shell to move the item to the recycle bin; if the item is too
// large, on a network share, or the shell refuses, the API reports success
// with fAnyOperationsAborted or falls back silently — we detect the common
// refusal cases and report (false, nil) so the caller refuses instead of
// hard-deleting without consent.

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// SHFILEOPSTRUCTW mirrors the Win32 struct (unicode layout).
type shfileopstructw struct {
	hwnd                  syscall.Handle
	wFunc                 uint16
	pFrom                 *uint16
	pTo                   *uint16
	fFlags                uint16
	fAnyOperationsAborted bool
	hNameMappings         uintptr
	lpszProgressTitle     *uint16
}

const (
	foDelete          = 0x0003
	fofAllowUndo      = 0x0040
	fofSilent         = 0x0400
	fofNoConfUI       = 0x10
	fofSimpleProgress = 0x200
)

var modshell32 = windows.NewLazySystemDLL("shell32.dll")

func trashPath(abs string) (movedToRecycle bool, err error) {
	// pFrom must be a double-NUL terminated list of paths.
	from, err := syscall.UTF16PtrFromString(abs + "\x00")
	if err != nil {
		return false, err
	}
	op := shfileopstructw{
		hwnd:   0,
		wFunc:  foDelete,
		pFrom:  from,
		pTo:    nil,
		fFlags: fofAllowUndo | fofSilent | fofNoConfUI | fofSimpleProgress,
	}
	ret, _, callErr := modshell32.NewProc("SHFileOperationW").Call(uintptr(unsafe.Pointer(&op)))
	if ret != 0 {
		// User aborted or the shell refused (e.g. beyond-quota). Treat a
		// clean abort as "no recycle bin available for this item".
		if op.fAnyOperationsAborted || ret == 0x71 { // FOE_ABORT
			return false, nil
		}
		return false, callErr
	}
	if op.fAnyOperationsAborted {
		return false, nil
	}
	return true, nil
}
