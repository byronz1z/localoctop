package main

import (
	"embed"
	"os"
	"strings"

	"github.com/byronz1z/localoctop"
	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	"github.com/wailsapp/wails/v2/pkg/options/windows"

	"github.com/byronz1z/localoctop/desktop/internal/tray"
)

//go:embed all:frontend/dist
var assets embed.FS

// localoctopVersion is the bridge core version shown in the tray tooltip.
func localoctopVersion() string { return localoctop.Version }

// startMinimized reports whether this process was launched with
// --minimized (开机自启 / autostart.go 写进 HKCU Run 的那个参数): the window
// stays hidden and only the tray icon shows — 开机启动不该弹窗抢焦点。
// Accepts "--minimized" and "-minimized" (cmd.exe quirk: some launch paths
// normalize to single dash), case-insensitive, only as a standalone arg.
func startMinimized() bool {
	for _, arg := range os.Args[1:] {
		a := strings.ToLower(strings.TrimSpace(arg))
		if a == "--minimized" || a == "-minimized" {
			return true
		}
	}
	return false
}

// secondDesktopAction is what a second launch should do, decided purely
// from "what is running" + "what am I". Kept as an enum so the decision is
// unit-testable without touching Win32.
type secondDesktopAction int

const (
	actBringToFront secondDesktopAction = iota // raise the running Wails window
	actMessageBox                              // tell the user, then exit
	actStderrExit                              // print to stderr, then exit
)

func (a secondDesktopAction) String() string {
	switch a {
	case actBringToFront:
		return "bring-to-front"
	case actMessageBox:
		return "message-box"
	default:
		return "stderr-exit"
	}
}

// decideSecondDesktopAction picks the action for a launch that found the
// shared lock already held. running is what ProbeSingleInstance reported for
// the holder; secondIsGUI is true when the second launch is the Wails
// desktop app (has a window it could raise), false for a background bridge.
func decideSecondDesktopAction(running localoctop.ProbeResult, secondIsGUI bool) secondDesktopAction {
	switch running {
	case localoctop.ProbeDesktop:
		// Another desktop instance is up. A second desktop launch lets Wails
		// raise the window; a background launch just exits with the stderr
		// line (it has no window to raise and no user watching for one).
		if secondIsGUI {
			return actBringToFront
		}
		return actStderrExit
	default:
		// A background program holds the lock. A second desktop launch shows
		// a message box (a bare exit would read as a crash); a second
		// background launch prints the stderr line.
		if secondIsGUI {
			return actMessageBox
		}
		return actStderrExit
	}
}

// followUpAction is what the second launch actually does after it decides
// to act. Kept as an enum so the choice is unit-testable without touching
// Win32.
type followUpAction int

const (
	followUpBroughtToFront followUpAction = iota // called BringWailsSingleInstanceWindowToFront, it worked
	followUpMessageBox                           // bring-to-front failed (or not applicable), fell back to a message box
	followUpNone                                 // nothing further to do (stderr-only paths)
)

// runSecondDesktopFollowUp performs the post-decision action for a second
// desktop launch. When the running instance is a desktop app we try to raise
// its window; if that fails (window gone, access denied, non-Windows) we fall
// back to a message box so the user always sees some feedback. bringToFront
// is injected for testing; production passes localoctop.BringWailsSingleInstanceWindowToFront.
func runSecondDesktopFollowUp(running localoctop.ProbeResult, bringToFront func() bool) followUpAction {
	if running != localoctop.ProbeDesktop {
		return followUpNone
	}
	if bringToFront != nil && bringToFront() {
		return followUpBroughtToFront
	}
	localoctop.ShowAlreadyRunningMessageBox()
	return followUpMessageBox
}

func main() {
	// Single instance across ALL localoctop entry points (bridge headless,
	// bridge desktop, this Wails desktop): one shared per-user lock. If the
	// lock is already held we do NOT start. When the holder is another
	// desktop instance we bring its window to front ourselves (Wails' own
	// single-instance forwarding does not run because we exit before
	// wails.Run). When the holder is a background program there is no window
	// to raise, so we tell the user in a message box and exit.
	if release, ok := localoctop.AcquireSingleInstance(); ok {
		defer release()
	} else {
		// Lock held. Decide what to do, then do it; every branch ends in
		// this process exiting without creating a second instance.
		probe := localoctop.ProbeSingleInstance()
		switch decideSecondDesktopAction(probe, true) {
		case actBringToFront:
			// Another desktop instance is running. Raise its window directly;
			// if that fails, fall back to a message box so the user is never
			// left with no visible feedback.
			runSecondDesktopFollowUp(probe, localoctop.BringWailsSingleInstanceWindowToFront)
		case actMessageBox:
			// A background / tray bridge holds the lock and has no window.
			localoctop.ShowAlreadyRunningMessageBox()
		case actStderrExit:
			// Should not happen for the GUI, but keep the path total.
			println("localoctop is already running on this computer; not starting a second copy")
		}
		return
	}

	// Create an instance of the app structure
	app := NewApp()

	// Register the tray BEFORE wails.Run, from this main goroutine: systray's
	// Register() creates the tray's hidden window on the calling thread and
	// starts no loop of its own; this goroutine is already locked to the
	// process's main OS thread (winc's init runs on first import of the
	// Wails Windows frontend — which options/windows triggers — and calls
	// runtime.LockOSThread). Wails' own GetMessage(NULL) main loop then pumps
	// the tray window's messages alongside its own. See internal/tray's
	// package comment for the full verified reasoning.
	tray.Register(localoctopVersion(), tray.Actions{
		OpenWindow: app.showFromTray,
		Quit:       app.quitFromTray,
	})

	// --minimized (开机自启): start hidden in the tray. HiddenAtStartup keeps
	// the Wails window off-screen from the first frame (no flash), the taskbar
	// entry absent; the tray menu's "打开窗口" shows it on demand.
	startHidden := startMinimized()

	// Create application with options
	err := wails.Run(&options.App{
		Title:     "localoctop",
		Width:     1024,
		Height:    768,
		MinWidth:  640,
		MinHeight: 480,
		AssetServer: &assetserver.Options{
			Assets: assets,
		},
		BackgroundColour: &options.RGBA{R: 245, G: 246, B: 248, A: 1},
		StartHidden:     startHidden, // 0.6.0: --minimized → tray-only start
		OnStartup:       app.startup,
		OnShutdown:      app.shutdown,
		OnBeforeClose:   app.onBeforeClose, // close-to-tray (M3)
		Bind: []interface{}{
			app,
		},
		// windowsgui, no console flash; closing the window hides to tray
		// (the tray's "退出" or a confirmed quit is the real exit path).
		Windows: &windows.Options{
			WebviewIsTransparent: false,
			WindowIsTranslucent:  false,
		},
		// Same UniqueId as the shared mutex name in single_instance.go, so
		// every entry point agrees on the identity of "the one desktop
		// instance". The first launch (which holds the shared lock) keeps this;
		// a second launch that got here via the ProbeDesktop branch lets
		// Wails raise the running window and then exits.
		SingleInstanceLock: &options.SingleInstanceLock{
			UniqueId: localoctop.SingleInstanceLockName(),
		},
	})

	if err != nil {
		println("Error:", err.Error())
	}
}
