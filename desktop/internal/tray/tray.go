// Package tray is the desktop shell's system-tray half: an icon with the
// live connection state, "打开窗口", and "退出". It wraps fyne.io/systray —
// the same library the main repo's console shell uses — which on Windows is
// pure syscall (no cgo), so the single-EXE build stays `go build`.
//
// Coexistence with Wails (the M3 headline risk, verified against both
// libraries' sources before writing this):
//
//   - systray.Register(onReady, onExit) is the documented entry point for
//     embedding in "other UI elements, for example, webview". Unlike Run()
//     it does NOT start a message loop; it synchronously creates the tray's
//     hidden message-only window (registerSystray → initInstance →
//     CreateWindowEx → Shell_NotifyIcon) on the CALLING thread.
//   - Wails' winc package locks the main goroutine to the process's main OS
//     thread (winc/app.go init: runtime.LockOSThread) and runs its message
//     loop there (winc.RunMainLoop: GetMessage(m, 0, 0, 0) with a NULL hwnd
//     — i.e. retrieve messages for ANY window of this thread).
//   - Therefore calling systray.Register from the Wails OnStartup hook —
//     which Wails invokes on that same main thread (frontend.go runs
//     OnStartup in a goroutine, but every window operation the app performs
//     marshals back to the locked thread via ControlBase.Invoke) — would
//     leave the tray window thread-ambiguous. Instead we call Register
//     BEFORE wails.Run, from the main goroutine: at that point the goroutine
//     is already the process's main thread (Go runtime convention + winc's
//     init has locked it), the tray window is created on exactly the thread
//     whose queue winc's GetMessage loop pumps, and every subsequent tray
//     message (WM_COMMAND menu clicks, taskbar notifications) is dispatched
//     by that very loop to systray's own wndProc.
//   - PreTranslateMessage only intercepts keyboard/mouse message ranges, so
//     the tray window's WM_COMMAND / custom tray messages pass through
//     untouched.
//
// Menu clicks are delivered on systrayMenuItemSelected → ClickedCh (a
// non-blocking send with a default drop), and this package's own goroutine
// converts them into Actions callbacks. Menu-item mutation (SetTitle /
// AddMenuItem) is mutex-guarded inside systray and safe from any goroutine.
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
	OpenWindow func() // "打开窗口" → runtime.WindowShow (+ un-minimise)
	Quit       func() // "退出"     → real shutdown (bridge cancel + runtime.Quit)
}

var (
	statusItem *systray.MenuItem // disabled row showing connection state
	actions    Actions
)

// Register sets up the tray icon and menu WITHOUT starting any message loop
// (the Wails main loop pumps it). Call it from the main goroutine before
// wails.Run — see the package comment for the thread reasoning.
func Register(version string, a Actions) {
	actions = a
	systray.Register(func() { setup(version) }, func() {})
}

// setup builds the menu; runs on systray's ready goroutine.
func setup(version string) {
	systray.SetIcon(iconICO)
	systray.SetTooltip(fmt.Sprintf("Octop 本地文件桥 v%s — 未连接", version))

	statusItem = systray.AddMenuItem("未连接", "桥连接状态")
	statusItem.Disable()
	systray.AddSeparator()
	mOpen := systray.AddMenuItem("打开窗口", "显示主窗口")
	systray.AddSeparator()
	mQuit := systray.AddMenuItem("退出", "断开桥并退出程序")

	go func() {
		for {
			select {
			case <-mOpen.ClickedCh:
				if actions.OpenWindow != nil {
					actions.OpenWindow()
				}
			case <-mQuit.ClickedCh:
				if actions.Quit != nil {
					actions.Quit()
				}
				systray.Quit() // removes the tray icon; loop WM_QUIT is inert (Wails owns it)
				return
			}
		}
	}()
}

// SetStatus updates the status row and tooltip. Safe from any goroutine.
func SetStatus(version string, connected bool, clientID string) {
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
	systray.SetTooltip(fmt.Sprintf("Octop 本地文件桥 v%s — %s", version, state))
}
