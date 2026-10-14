//go:build windows

package localoctop

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	kernel32          = windows.NewLazySystemDLL("kernel32.dll")
	procCreateMutexW  = kernel32.NewProc("CreateMutexW")
	procCloseHandle   = kernel32.NewProc("CloseHandle")
	user32            = windows.NewLazySystemDLL("user32.dll")
	procFindWindowW   = user32.NewProc("FindWindowW")
	procSetForeground = user32.NewProc("SetForegroundWindow")
	procShowWindow    = user32.NewProc("ShowWindow")
	procIsIconic      = user32.NewProc("IsIconic")
	procBringWindowToT = user32.NewProc("BringWindowToTop")
	procMessageBoxW   = user32.NewProc("MessageBoxW")
)

const (
	swRestore = 9
)

// windowsCreateMutex is the raw CreateMutexW call. It returns the handle and
// whether the mutex already existed (ERROR_ALREADY_EXISTS).
func windowsCreateMutex(name *uint16) (uintptr, bool) {
	h, _, err := procCreateMutexW.Call(0, 0, uintptr(unsafe.Pointer(name)))
	if h == 0 {
		return 0, false
	}
	return h, err == windows.ERROR_ALREADY_EXISTS
}

// windowsFindWindow looks up a top-level window by class (and optionally
// name). Returns the HWND, or 0 if none.
func windowsFindWindow(class *uint16) uintptr {
	hwnd, _, _ := procFindWindowW.Call(uintptr(unsafe.Pointer(class)), 0)
	return hwnd
}

// windowsFindWindowByClassAndName is FindWindowW with both class and name.
func windowsFindWindowByClassAndName(class, name *uint16) uintptr {
	hwnd, _, _ := procFindWindowW.Call(uintptr(unsafe.Pointer(class)),
		uintptr(unsafe.Pointer(name)))
	return hwnd
}

// BringWailsSingleInstanceWindowToFront restores (if minimised) and raises
// the running Wails desktop's main window. It is called by a second desktop
// launch after it finds the single-instance window. Returns true on success.
func BringWailsSingleInstanceWindowToFront() bool {
	class, err := windows.UTF16PtrFromString(wailsSingleInstanceClass)
	if err != nil {
		return false
	}
	hwnd := windowsFindWindow(class)
	if hwnd == 0 {
		return false
	}
	// Wails' single-instance handler does exactly this on its side; the
	// second instance mirrors it: restore if minimised, then force to front.
	iconic, _, _ := procIsIconic.Call(hwnd)
	if iconic != 0 {
		procShowWindow.Call(hwnd, swRestore)
	}
	procBringWindowToT.Call(hwnd)
	procSetForeground.Call(hwnd)
	return true
}

// ShowAlreadyRunningMessageBox shows a plain message box telling the user
// localoctop is already running, and blocks until they dismiss it. It is
// used by the desktop app when the lock is held by a background program (no
// window to bring forward, and a silent exit would read as a crash).
func ShowAlreadyRunningMessageBox() {
	title, _ := windows.UTF16PtrFromString("localoctop")
	text, _ := windows.UTF16PtrFromString(
		"localoctop is already running on this computer. " +
			"Close it first, then start this program again.")
	procMessageBoxW.Call(0, uintptr(unsafe.Pointer(text)),
		uintptr(unsafe.Pointer(title)), 0x00000040) // MB_ICONINFORMATION
}

// windowsCloseHandle closes a handle returned by windowsCreateMutex.
func windowsCloseHandle(h uintptr) {
	procCloseHandle.Call(h)
}
