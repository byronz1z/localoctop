//go:build windows

package localoctop

// Windows recycle-bin support via the shell operation API. FOF_ALLOWUNDO
// asks the shell to move the item to the recycle bin. When the shell refuses
// (network share, beyond quota, FOE_CANTUNDO) we report (false, nil) so the
// caller refuses instead of silently hard-deleting — the employee's trash
// stays recoverable, and only permanent=true forces the irreversible path.

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// shfileopstructw mirrors the Win32 SHFILEOPSTRUCTW with EXACT field widths:
// HWND (ptr), UINT, two PCZZWSTR, FILEOP_FLAGS (BOOL=32-bit), BOOL, LPVOID,
// PCZZWSTR. Get the layout right or the API reads misaligned fields. On
// amd64 the struct is: 8 + 4(pad4) + 8 + 8 + 4 + 4 + 8 + 8.
type shfileopstructw struct {
	hwnd                  uintptr // HWND
	wFunc                 uint32  // UINT
	_                     [4]byte // padding for pointer alignment of pFrom
	pFrom                 *uint16 // PCZZWSTR (double-NUL list)
	pTo                   *uint16 // PCZZWSTR
	fFlags                uint32  // FILEOP_FLAGS (BOOL-sized)
	fAnyOperationsAborted uint32  // BOOL
	hNameMappings         uintptr // LPVOID
	lpszProgressTitle     *uint16 // PCZZWSTR
}

const (
	foDelete         = 0x0003
	fofSilent        = 0x0004 // no progress dialog
	fofNoConfUI      = 0x0010 // FOF_NOCONFIRMATION
	fofAllowUndo     = 0x0040 // recycle bin
	fofNoConfirMKDir = 0x0200 // no "confirm mkdir" prompts
	fofNoErrorUI     = 0x0400 // no error dialogs (we surface them ourselves)
	foeAbort         = 0x0071 // 113
	foeCantUndo      = 0x007C // 124
)

var modshell32 = windows.NewLazySystemDLL("shell32.dll")

func trashPath(abs string) (movedToRecycle bool, err error) {
	// pFrom is a double-NUL terminated list. syscall.UTF16PtrFromString
	// REJECTS embedded NULs, so build via UTF16FromString (which appends a
	// single terminating NUL) and add one more NUL for the list terminator.
	utf16, err := syscall.UTF16FromString(abs)
	if err != nil {
		return false, err
	}
	buf := append(utf16, 0)
	op := shfileopstructw{
		hwnd:   0,
		wFunc:  foDelete,
		pFrom:  &buf[0],
		pTo:    nil,
		fFlags: fofSilent | fofNoConfUI | fofNoConfirMKDir | fofNoErrorUI | fofAllowUndo,
	}
	// SHFileOperationW returns an int error code (0 = success).
	ret, _, _ := modshell32.NewProc("SHFileOperationW").Call(uintptr(unsafe.Pointer(&op)))
	switch {
	case ret == 0 && op.fAnyOperationsAborted == 0:
		return true, nil
	case ret == foeCantUndo || ret == foeAbort || op.fAnyOperationsAborted != 0:
		// The shell won't/can't recycle this item — not an error, but "no
		// recycle bin available". removeOne refuses unless permanent=true.
		return false, nil
	case ret != 0:
		return false, syscall.Errno(ret)
	default:
		return false, nil
	}
}
