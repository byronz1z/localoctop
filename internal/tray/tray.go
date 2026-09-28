// Package tray is the system-tray half of the desktop shell: an icon with
// connection status, "open console", and "quit". It wraps fyne.io/systray,
// which is pure syscall on Windows (no cgo), so the single-EXE build stays
// `go build` with no C toolchain.
package tray

import (
	_ "embed"
	"fmt"

	"fyne.io/systray"
)

//go:embed icon.ico
var iconICO []byte

// Actions carries the callbacks the app wires to the tray menu.
type Actions struct {
	OpenConsole func()
	Quit        func()
}

// statusItem is the disabled menu row showing connection state; set in setup.
var statusItem *systray.MenuItem

// Run blocks running the tray icon until Quit is clicked (or systray is
// otherwise stopped). Must be called from the main goroutine on Windows.
func Run(consoleURL, version string, a Actions) {
	systray.Run(func() { setup(consoleURL, version, a) }, func() {})
}

// Quit programmatically stops the tray loop (used when the app shuts down
// from a signal rather than the tray menu).
func Quit() { systray.Quit() }

func setup(consoleURL, version string, a Actions) {
	systray.SetIcon(iconICO)
	systray.SetTitle("Octop Local Bridge")
	systray.SetTooltip(fmt.Sprintf("Octop Local Bridge v%s — %s", version, consoleURL))

	statusItem = systray.AddMenuItem("未连接", "bridge connection state")
	statusItem.Disable()
	systray.AddSeparator()
	mOpen := systray.AddMenuItem("打开控制台", "open the local web console")
	systray.AddSeparator()
	mQuit := systray.AddMenuItem("退出", "stop the bridge and exit")

	go func() {
		for {
			select {
			case <-mOpen.ClickedCh:
				if a.OpenConsole != nil {
					a.OpenConsole()
				}
			case <-mQuit.ClickedCh:
				if a.Quit != nil {
					a.Quit()
				}
				systray.Quit()
				return
			}
		}
	}()
}

// SetStatus updates the status menu item and tooltip. Safe to call from any
// goroutine once Run has started.
func SetStatus(consoleURL, version string, connected bool, clientID string) {
	state := "未连接"
	if connected {
		state = "已连接"
	}
	text := state
	if clientID != "" {
		text += " · " + clientID
	}
	if statusItem != nil {
		statusItem.SetTitle(text)
	}
	systray.SetTooltip(fmt.Sprintf("Octop Local Bridge v%s — %s — %s", version, state, consoleURL))
}
