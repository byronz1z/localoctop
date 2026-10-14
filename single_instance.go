package localoctop

// Single-instance lock: only one localoctop program (bridge headless, bridge
// desktop, or Wails desktop) may run on this computer at a time. Windows uses
// a named mutex in the current user's session namespace; other systems use a
// lock file under the user's home directory. The lock is released when the
// process exits, however it exits.

// singleInstanceLockName is the per-user mutex / lock-file name shared by
// every localoctop entry point (bridge headless, bridge desktop, Wails
// desktop). The Wails desktop app derives its OWN single-instance names from
// the same UniqueId (mutex "wails-app-<id>sim", window class "...-sic",
// window name "...-siw"), so all three entry points can detect each other.
const singleInstanceLockName = "localoctop-singleton"

// SingleInstanceLockName returns the shared per-user lock name. The Wails
// desktop app uses it as SingleInstanceLock.UniqueId so Wails' own
// single-instance machinery and the shared mutex refer to the same identity.
func SingleInstanceLockName() string {
	return singleInstanceLockName
}

// AcquireSingleInstance takes the per-user lock. It returns a release
// function on success. It returns false (and a no-op release) when another
// localoctop process already holds the lock.
func AcquireSingleInstance() (func(), bool) {
	return acquireSingleInstanceImpl()
}

// ProbeResult tells a contending entry point what the lock holder is, so it
// can take the right action (bring the running desktop window to front, or
// just exit with a message).
type ProbeResult int

const (
	// ProbeNone: no other localoctop program is running (the caller is the
	// first instance).
	ProbeNone ProbeResult = iota
	// ProbeDesktop: another instance is running AND it is the Wails desktop
	// app (its hidden single-instance window exists). A second desktop
	// launch should ask it to come to front and then exit.
	ProbeDesktop
	// ProbeBackground: another instance is running but it is NOT the Wails
	// desktop app (a background / tray bridge holds the lock, which never
	// creates a Wails window). A second launch should just refuse to start.
	ProbeBackground
)

// ProbeSingleInstance checks whether another localoctop program already
// holds the single-instance lock, and if so whether it is the Wails desktop
// app. It never takes the lock and never blocks. Callers that got false from
// AcquireSingleInstance use this to decide what to do next.
func ProbeSingleInstance() ProbeResult {
	return probeSingleInstanceImpl()
}
