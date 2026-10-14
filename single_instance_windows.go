package localoctop

import (
	"golang.org/x/sys/windows"
)

// acquireSingleInstanceImpl takes a named mutex in the current user's
// session namespace. The name has no "Global\" prefix, so it is per-user
// (session-local) and does not collide with other users on the same machine.
// The OS releases the mutex automatically when the process handle is closed
// (i.e. on any exit, including kill).
func acquireSingleInstanceImpl() (func(), bool) {
	name, err := windows.UTF16PtrFromString(singleInstanceLockName)
	if err != nil {
		return func() {}, false
	}
	h, already := windowsCreateMutex(name)
	if h == 0 {
		return func() {}, false
	}
	if already {
		windowsCloseHandle(h)
		return func() {}, false
	}
	return func() { windowsCloseHandle(h) }, true
}

// wailsSingleInstanceClass is the Win32 window class Wails v2.10.1 registers
// for its single-instance hidden window. It is derived from the same
// UniqueId the desktop app passes as SingleInstanceLock.UniqueId:
//
//	id        = "wails-app-" + uniqueId
//	className = id + "-sic"
//	windowName= id + "-siw"
//
// The desktop app sets UniqueId to singleInstanceLockName, so the class is
// exactly the constant below. The window is created by the FIRST Wails
// instance and lives for as long as it runs, so finding it means a desktop
// app is up — regardless of which entry point is asking.
const wailsSingleInstanceClass = "wails-app-localoctop-singleton-sic"

// wailsSingleInstanceWindow is the window name of the same hidden window.
const wailsSingleInstanceWindow = "wails-app-localoctop-singleton-siw"

// probeSingleInstanceImpl reports whether the shared lock is held, and if so
// whether the holder is the Wails desktop app. It detects the desktop by
// looking for Wails' own single-instance hidden window (created only by a
// running Wails instance). A background bridge holds the same mutex but
// never creates that window.
func probeSingleInstanceImpl() ProbeResult {
	release, ok := AcquireSingleInstance()
	if ok {
		release()
		return ProbeNone
	}
	// The lock is held. Look for Wails' single-instance window.
	class, err := windows.UTF16PtrFromString(wailsSingleInstanceClass)
	if err != nil {
		return ProbeBackground
	}
	name, err := windows.UTF16PtrFromString(wailsSingleInstanceWindow)
	if err != nil {
		name = nil
	}
	hwnd := windowsFindWindowByClassAndName(class, name)
	if hwnd != 0 {
		return ProbeDesktop
	}
	// Fall back to class-only lookup (the window name can be empty on some
	// Wails builds).
	hwnd = windowsFindWindow(class)
	if hwnd != 0 {
		return ProbeDesktop
	}
	return ProbeBackground
}
