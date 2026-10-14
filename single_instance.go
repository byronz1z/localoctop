package localoctop

// Single-instance lock: only one localoctop program (bridge headless, bridge
// desktop, or Wails desktop) may run on this computer at a time. Windows uses
// a named mutex in the current user's session namespace; other systems use a
// lock file under the user's home directory. The lock is released when the
// process exits, however it exits.

const singleInstanceLockName = "localoctop-singleton"

// AcquireSingleInstance takes the per-user lock. It returns a release
// function on success. It returns false (and a no-op release) when another
// localoctop process already holds the lock.
func AcquireSingleInstance() (func(), bool) {
	return acquireSingleInstanceImpl()
}
