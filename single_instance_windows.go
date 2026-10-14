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
	h, err := windows.CreateMutex(nil, false, name)
	if err != nil {
		return func() {}, false
	}
	if windows.GetLastError() == windows.ERROR_ALREADY_EXISTS {
		windows.CloseHandle(h)
		return func() {}, false
	}
	return func() { windows.CloseHandle(h) }, true
}
