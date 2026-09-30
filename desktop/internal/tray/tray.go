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
//
// 0.6.0 (T2): SetStatus takes the app's conn_state string so the tray row
// and tooltip mirror the same states the status card renders
// (已连接 / 连接中 / 重连中 第 N 次 / 已断开 / 未配置) instead of the old
// binary 已连接-未连接.
//
// 0.6.1 (T3'): sixth state parked — the token was taken over by another
// device (server close code 4000, bridge core parks instead of fighting).
// Tray says 已在别处登录, same words as the status card.
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
	statusItem *systray.MenuItem // disabled row displaying connection state
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
	systray.SetTooltip(fmt.Sprintf("localoctop v%s — 未连接", version))

	statusItem = systray.AddMenuItem("未连接", "连接状态")
	statusItem.Disable()
	systray.AddSeparator()
	mOpen := systray.AddMenuItem("打开窗口", "显示主窗口")
	systray.AddSeparator()
	mQuit := systray.AddMenuItem("退出", "退出并停止文件访问")

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

// TrayLabel maps the app's conn_state to the tray row text and tooltip
// tail. Kept in sync with main.js's renderStatus and docs/UI-COPY.md —
// same six states, same words, no engineering metrics (attempt counts
// stay in logs; the tray row just says 重连中…).
func TrayLabel(connState string, connected bool, attempt int) (row, tail string) {
	switch connState {
	case "connected":
		return "已连接", "已连接"
	case "connecting":
		return "连接中…", "连接中"
	case "reconnecting":
		return "重连中…", "重连中"
	case "parked":
		// 被接管停泊：同令牌另一台设备在线，本机暂停；「连接」收回。
		return "已在别处登录", "已在别处登录"
	case "disconnected":
		return "已断开", "已断开"
	default: // "unconfigured" / pre-startup ""
		if connected { // defensive: state lagging behind the bool
			return "已连接", "已连接"
		}
		return "未配置", "未配置"
	}
}

// SetStatus updates the status row and tooltip from the app's six-state
// snapshot. Safe from any goroutine.
func SetStatus(version, connState string, connected bool, attempt int, clientID string) {
	row, tail := TrayLabel(connState, connected, attempt)
	text := row
	if clientID != "" && connState == "connected" {
		text += " · " + clientID
	}
	if statusItem != nil {
		statusItem.SetTitle(text)
	}
	systray.SetTooltip(fmt.Sprintf("localoctop v%s — %s", version, tail))
}
