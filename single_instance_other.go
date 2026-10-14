//go:build !windows

package localoctop

import (
	"os"
	"path/filepath"
	"syscall"
)

// acquireSingleInstanceImpl takes an exclusive non-blocking lock on a file
// under the user's home directory. The OS releases the lock when the file
// descriptor is closed (i.e. on any exit, including kill). Per-user because
// the file lives in the user's own directory.
func acquireSingleInstanceImpl() (func(), bool) {
	dir, err := os.UserHomeDir()
	if err != nil {
		return func() {}, false
	}
	dir = filepath.Join(dir, ".localoctop")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return func() {}, false
	}
	path := filepath.Join(dir, singleInstanceLockName+".lock")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return func() {}, false
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return func() {}, false
	}
	return func() { f.Close() }, true
}
