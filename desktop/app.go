// app.go is the Wails-bound application core of localoctop-desktop: it owns
// the settings file (appcfg, shared with the main-repo bridge EXE), the
// running bridge instance, the tray integration (M3), and the status/audit
// state the window UI renders.
//
// The bridge lifecycle mirrors the main repo's cmd/bridge/main.go desktop
// mode: Load on startup → build Bridge → go b.Run(ctx); every save
// re-validates, persists, then swaps the bridge — a config that fails
// validation or bridge construction returns the error and leaves the
// previous bridge running (rebuildLocked semantics, copied 1:1).
//
// M3 adds the resident-tray lifecycle: closing the window hides to tray
// (onBeforeClose intercepts), and the tray's "退出" is the only real exit
// path (plus an explicit "真的退出" answer in the close-confirm dialog).
//
// 0.6.0 (T2) makes the connection state REAL and user-steerable:
//
//   - five-state status card fed by the bridge core's OnStatusDetail
//     (0.6.1 adds the sixth state, parked — 已在别处登录, surfaced once the
//     bridge core reports token takeover; the constant + UI branches are
//     in place ahead of the core's T2' landing)
//     (StatusDetail: connected / reconnect attempt / last error / ping-pong
//     timestamps) plus a 3s polling fallback — the old Connected bool was a
//     lie (it showed the last dial result; a dead session read "已连接");
//   - manual 断开/连接: Disconnect() cancels the bridge AND raises the
//     userIntent flag so neither the 3s poller nor a later save silently
//     reconnects over the user's choice; Connect()/SaveConfig rebuild from
//     config and clear the flag;
//   - all UI pushes go through emitStatusUnlocked, which App.mu callers
//     invoke WITHOUT the lock (the pre-T2 emitStatus re-locked a.mu and
//     deadlocked every synchronous SaveConfig — fixed here, verified by
//     app_test.go's no-deadlock case).
package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/byronz1z/localoctop"
	"github.com/wailsapp/wails/v2/pkg/runtime"

	"github.com/byronz1z/localoctop/internal/appcfg"
	"github.com/byronz1z/localoctop/desktop/internal/tray"
)

// connState is the connection lifecycle the six-state card renders.
type connState string

const (
	stateUnknown     connState = ""            // before startup: transient
	stateConnecting  connState = "connecting"  // bridge built, first dial in flight
	stateConnected   connState = "connected"   // Connected=true
	stateReconnecting connState = "reconnecting" // attempt>0 after a drop
	stateParked      connState = "parked"      // 0.6.1: token taken over elsewhere (server 4000), auto-reconnect parked
	stateDisconnected connState = "disconnected" // user-initiated, stays down
	stateUnconfigured connState = "unconfigured" // no server URL/token on file
)

// statusPollInterval is the fallback cadence for pulling
// Bridge.StatusDetail() and re-emitting: catches anything the
// OnStatusDetail push missed (e.g. the >90s stale-pong warning flipping on
// with no event, since LastPongAt only changes on the next pong).
const statusPollInterval = 3 * time.Second

// stalePongAfter: connected but no pong for this long → 心跳异常 warning.
// PongWait in the core is 60s; 90s gives one full missed ping before alarm.
const stalePongAfter = 90 * time.Second

// App is the single struct bound to the frontend (window.go.main.App).
type App struct {
	ctx context.Context // set by startup; needed for the native dialog

	mu            sync.Mutex  // guards everything below
	cfg           appcfg.File // current settings (mirror of what's on disk)
	configExisted bool        // false = first start (file was created from defaults)
	bridge        *localoctop.Bridge
	cancel        context.CancelFunc // stops the current bridge's Run
	quitting      bool               // user has decided to exit: onBeforeClose stops intercepting
	userDisconnected bool            // manual 断开 in effect: no auto-reconnect until Connect/Save

	st statusState // live connection state fed by OnStatusDetail/OnAudit
}

// statusState is the mutex-guarded holder behind Status snapshots. The mutex
// lives on App (not on Status) so returning snapshots never copies a lock
// (go vet copylocks), matching the main repo console's pattern.
type statusState struct {
	cur Status
}

// Status is the structured connection state the UI banner renders. The
// 0.6.0 T2 additions (conn_state, reconnect_attempt, last_pong_at) come
// straight from the bridge core's StatusDetail; legacy fields stay for the
// header dot and stats row.
type Status struct {
	Connected  bool   `json:"connected"`
	ConnState  string `json:"conn_state"`           // six-state card driver (connState values)
	ClientID   string `json:"client_id"`
	ServerURL  string `json:"server_url"`
	LastError  string `json:"last_error,omitempty"`
	Reconnects int    `json:"reconnects"`
	ReconnectAttempt int `json:"reconnect_attempt"` // current attempt # from StatusDetail
	LastPongAt  time.Time `json:"last_pong_at"`     // zero = never; drives the >90s warning
	LastAudit  string `json:"last_audit_ts,omitempty"`
}

// CommonDir is one "frequently used location" offered for one-click adding
// (same shape as the main repo console's CommonDir).
type CommonDir struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// SettingsInfo is the read-only "关于/路径" block the settings page renders.
type SettingsInfo struct {
	Version      string `json:"version"`        // localoctop.Version (bridge core)
	ConfigPath   string `json:"config_path"`    // appcfg.Path()
	AuditLogPath string `json:"audit_log_path"` // effective audit log location
}

// NewApp creates the application. The bridge itself is built in startup
// (needs the Wails context for event emission).
func NewApp() *App {
	return &App{}
}

// startup is the Wails OnStartup hook: save the context, then build the
// first bridge from the loaded settings. An unconfigured install (or a
// config that fails validation) builds nothing — the UI shows the
// onboarding form instead, exactly like the main repo's console flow.
func (a *App) startup(ctx context.Context) {
	a.ctx = ctx

	cfg, existed, err := appcfg.Load()
	if err != nil {
		// Not fatal: the form can still save later. Surface via status.
		a.st.update(func(s *Status) { s.LastError = "读取配置失败: " + err.Error() })
	}
	if !existed {
		if err := appcfg.Save(cfg); err == nil {
			// seed the file so desktop and old EXE share one config
		}
	}

	a.mu.Lock()
	a.cfg = cfg
	a.configExisted = existed
	a.mu.Unlock()

	// First bridge from the loaded settings (a.st is wired inside
	// rebuildLocked so even startup errors reach the UI). This is the
	// zero-operation auto-connect: it only happens when the user has NOT
	// disconnected manually this session — startup is always a fresh
	// session, so userDisconnected is false here by construction.
	_ = a.rebuild(cfg)
	a.emitStatusUnlocked()

	// 3s polling fallback: pushes refreshes (stale-pong warnings flip on
	// wall-clock, not events) and re-emits when OnStatusDetail was missed.
	go a.statusPollLoop()
}

// statusPollLoop is the belt-and-suspenders half of the five-state card:
// OnStatusDetail pushes every change, but some card-affecting facts only
// change with the clock (a connected session whose pongs stopped 90s ago)
// or might race the emitter (bridge swapped mid-push). Pull the snapshot,
// fold it in, re-emit. Exits with the app context (Wails cancels it on
// shutdown); the bridge pointer is read under App.mu.
func (a *App) statusPollLoop() {
	t := time.NewTicker(statusPollInterval)
	defer t.Stop()
	for {
		select {
		case <-a.ctx.Done():
			return
		case <-t.C:
			a.mu.Lock()
			b := a.bridge
			a.mu.Unlock()
			if b == nil {
				continue // unconfigured / user-disconnected: nothing to poll
			}
			a.applyDetail(b.StatusDetail())
			a.emitStatusUnlocked()
		}
	}
}

// onBeforeClose is the Wails OnBeforeClose hook: closing the window hides to
// tray instead of exiting — the tray's "退出" (or an explicit "是" in the
// confirm dialog) is the only path that ends the process. Wails invokes this
// on the main/UI thread; the native MessageBox confirm is modal there by
// design (MessageBox runs its own message pump, so the app stays live).
func (a *App) onBeforeClose(ctx context.Context) bool {
	a.mu.Lock()
	quitting := a.quitting
	confirm := a.cfg.ConfirmExit != nil && *a.cfg.ConfirmExit
	a.mu.Unlock()

	if !quitting && confirm && a.ctx != nil {
		choice, err := runtime.MessageDialog(a.ctx, runtime.MessageDialogOptions{
			Type:          runtime.QuestionDialog,
			Title:         "要退出程序吗？",
			Message:       "关闭窗口后程序将最小化到系统托盘继续运行，云端 AI 的文件访问保持可用。\n\n点「是」完全退出：连接将断开，云端 AI 无法再访问本机文件。",
			DefaultButton: "No", // Esc / default lands on "否" = 安全侧（留在托盘）
		})
		if err == nil && choice == "Yes" {
			// User truly wants out: let this close proceed as a real quit.
			a.mu.Lock()
			a.quitting = true
			a.mu.Unlock()
			return false
		}
		// "No" / Cancel / dialog error → fall through to hide-to-tray.
	}

	runtime.WindowHide(a.ctx)
	return true // prevent the default close; process stays resident
}

// quitFromTray is the tray "退出" handler: mark the exit as user-decided
// (so onBeforeClose stops intercepting), cancel the bridge so the server
// sees the session go offline, then quit the app for real.
func (a *App) quitFromTray() {
	a.mu.Lock()
	a.quitting = true
	a.mu.Unlock()

	a.shutdown(a.ctx)
	runtime.Quit(a.ctx)
}

// showFromTray is the tray "打开窗口" handler.
func (a *App) showFromTray() {
	runtime.WindowUnminimise(a.ctx)
	runtime.WindowShow(a.ctx)
}

// shutdown is the Wails OnShutdown hook: stop the bridge so the process
// exits cleanly and the server sees the session go offline.
func (a *App) shutdown(ctx context.Context) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.cancel != nil {
		a.cancel()
	}
}

func (s *statusState) snapshot() Status { return s.cur }

func (s *statusState) update(fn func(*Status)) { fn(&s.cur) }

// ---- bound methods (called from the frontend as window.go.main.App.X) ----

// LoadConfigResult bundles the settings with the first-run flag: Existed
// false means the config file was just created from defaults → the UI shows
// the onboarding form.
type LoadConfigResult struct {
	File    appcfg.File `json:"file"`
	Existed bool        `json:"existed"`
}

// LoadConfig reads the settings file (shared with the main-repo EXE) and
// reports whether it existed before this run (first-start detection).
func (a *App) LoadConfig() LoadConfigResult {
	a.mu.Lock()
	defer a.mu.Unlock()
	return LoadConfigResult{File: a.cfg, Existed: a.configExisted}
}

// SaveConfig validates next, persists it, and hot-rebuilds the bridge. On
// any failure the previous bridge keeps running and the error is returned
// for the UI to show (semantics copied from cmd/bridge's apply closure +
// rebuildLocked: validation/bridge failure → error, old bridge untouched).
//
// T2: saving is also an implicit (re)connect — it clears the manual-
// disconnect intent, because "改配置后保存" is the user asking to connect
// with the new settings (task spec: 断开只挡自动连接，不挡手动动作).
func (a *App) SaveConfig(next appcfg.File) error {
	a.mu.Lock()

	// Remember this connection (deduped, capped) so M2's server dropdown
	// has the data — same call the main console makes on save.
	cfg := a.cfg // preserve fields the UI omits (AuditLogPath etc.)
	cfg.ServerURL = next.ServerURL
	cfg.Token = next.Token
	cfg.AllowedDirs = next.AllowedDirs
	cfg.AllowWrite = next.AllowWrite
	cfg.RememberServer(next.ServerURL, next.Token)

	if err := appcfg.Save(cfg); err != nil {
		a.mu.Unlock()
		return err
	}
	a.cfg = cfg
	a.userDisconnected = false // save = connect intent
	if err := a.rebuildLocked(cfg); err != nil {
		a.mu.Unlock()
		return err
	}
	snap := a.st.snapshot()
	a.mu.Unlock()
	emitStatus(a.ctx, snap) // outside a.mu: the pre-T2 in-lock emit deadlocked
	return nil
}

// GetStatus returns the structured connection snapshot.
func (a *App) GetStatus() Status {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.st.snapshot()
}

// Connect rebuilds the bridge from the current config — the manual
// counterpart of Disconnect (settings page / status card button). Returns
// an error (and changes nothing) when the config does not validate, e.g.
// the unconfigured first run; the frontend gates the button on that state.
func (a *App) Connect() error {
	a.mu.Lock()
	if a.userDisconnected {
		a.userDisconnected = false
	}
	err := a.rebuildLocked(a.cfg)
	snap := a.st.snapshot()
	a.mu.Unlock()
	emitStatus(a.ctx, snap)
	return err
}

// Disconnect is the user's explicit 断开: cancel the bridge Run and switch
// the card to 已断开. The userDisconnected flag keeps every auto path
// (statusPollLoop, a later emit) from rebuilding until Connect or SaveConfig
// clears it — 断开是用户意志，自动连接不得覆盖.
func (a *App) Disconnect() {
	a.mu.Lock()
	a.userDisconnected = true
	if a.cancel != nil {
		a.cancel() // stops Run; the next poll sees bridge==nil-ish state below
	}
	a.bridge = nil
	a.cancel = nil
	a.st.update(func(s *Status) {
		s.Connected = false
		s.ConnState = string(stateDisconnected)
		s.ReconnectAttempt = 0
	})
	snap := a.st.snapshot()
	a.mu.Unlock()
	emitStatus(a.ctx, snap)
}

// PickDirectory opens the Wails-native directory chooser and returns the
// selected path ("" = cancelled). This replaces the main repo's PowerShell
// FolderBrowserDialog subprocess — the root fix for its GBK mojibake bug:
// no .ps1, no child process, no console window.
func (a *App) PickDirectory() string {
	path, err := runtime.OpenDirectoryDialog(a.ctx, runtime.OpenDialogOptions{
		Title: "选择白名单目录",
	})
	if err != nil {
		return "" // cancelled or unavailable: form stays untouched
	}
	return path
}

// GetCommonDirs lists the frequently-used locations that exist on this
// machine (user profile root, Desktop, Documents, Downloads, plus the
// OneDrive redirects when active) — logic mirrors the main repo's
// console.commonDirs.
func (a *App) GetCommonDirs() []CommonDir {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return []CommonDir{}
	}
	cands := []CommonDir{
		{"用户目录", home},
		{"桌面", filepath.Join(home, "Desktop")},
		{"文档", filepath.Join(home, "Documents")},
		{"下载", filepath.Join(home, "Downloads")},
	}
	if od := os.Getenv("OneDrive"); od != "" {
		cands = append(cands,
			CommonDir{"桌面 (OneDrive)", filepath.Join(od, "Desktop")},
			CommonDir{"文档 (OneDrive)", filepath.Join(od, "Documents")},
		)
	}
	var out []CommonDir
	seen := map[string]bool{}
	for _, c := range cands {
		key := strings.ToLower(filepath.Clean(c.Path))
		if seen[key] {
			continue
		}
		st, err := os.Stat(c.Path)
		if err != nil || !st.IsDir() {
			continue
		}
		seen[key] = true
		out = append(out, c)
	}
	if out == nil {
		out = []CommonDir{}
	}
	return out
}

// GetVersion returns the bridge core version (localoctop.Version).
func (a *App) GetVersion() string {
	return localoctop.Version
}

// GetSettingsInfo returns the read-only settings-page block: bridge version,
// config file path, and audit log path (M3 settings page).
func (a *App) GetSettingsInfo() SettingsInfo {
	info := SettingsInfo{Version: localoctop.Version}
	if p, err := appcfg.Path(); err == nil {
		info.ConfigPath = p
	}
	a.mu.Lock()
	info.AuditLogPath = a.cfg.AuditLogPath
	a.mu.Unlock()
	if info.AuditLogPath == "" {
		if p, err := appcfg.AuditPath(); err == nil {
			info.AuditLogPath = p
		}
	}
	return info
}

// GetConfirmExit reports whether the close-to-tray confirmation dialog is
// enabled (settings page toggle; the persistent "不再提示" control — the
// native MessageBox cannot host a checkbox, so this toggle is it).
func (a *App) GetConfirmExit() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.cfg.ConfirmExit != nil && *a.cfg.ConfirmExit
}

// SetConfirmExit persists the confirm-on-close preference and applies it to
// the running session (settings page toggle).
func (a *App) SetConfirmExit(on bool) error {
	a.mu.Lock()
	cfg := a.cfg
	cfg.ConfirmExit = &on
	if err := appcfg.Save(cfg); err != nil {
		return err
	}
	a.cfg = cfg
	a.mu.Unlock()
	return nil
}

// ---- bridge lifecycle ----

// rebuild builds a Bridge from cfg and swaps it in (unlocked variant for
// startup, before any UI is bound). Failure returns the error and leaves
// state untouched.
func (a *App) rebuild(cfg appcfg.File) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.rebuildLocked(cfg)
}

// rebuildLocked builds a Bridge from cfg and swaps it in, cancelling the
// previous one's Run. Callers hold a.mu. A config that does not validate
// (e.g. the unconfigured first run) or a bridge that cannot be built leaves
// the previous bridge running and returns the error — copied from
// cmd/bridge/main.go rebuildLocked.
func (a *App) rebuildLocked(cfg appcfg.File) error {
	bc := localoctop.NewConfig()
	bc.ServerURL = cfg.ServerURL
	bc.Token = cfg.Token
	bc.AllowedDirs = cfg.EnabledDirs()
	bc.AllowWrite = cfg.AllowWrite
	bc.AuditLogPath = cfg.AuditLogPath
	bc.Logger = localoctop.NewStderrLogger(false)

	// T2 status feed: the six-state card renders from StatusDetail pushes.
	// applyDetail takes a.mu itself, so this callback must NOT hold it —
	// rebuildLocked's callers (holding a.mu) never wait on a bridge callback
	// (OnStatusDetail fires from Run/ping goroutines), so no cycle.
	bc.OnStatusDetail = func(d localoctop.StatusDetail) {
		a.applyDetail(d)
		a.emitStatusUnlocked()
	}
	bc.OnAudit = func(ev localoctop.AuditEvent) {
		a.mu.Lock()
		a.st.update(func(s *Status) { s.LastAudit = ev.Time })
		a.mu.Unlock()
		a.emitAudit(ev)
	}

	b, err := localoctop.New(bc)
	if err != nil {
		return err
	}
	// New identity for the status snapshot + the connecting state: the card
	// shows 连接中 from here until the first StatusDetail push says online.
	a.st.update(func(s *Status) {
		s.ClientID = b.ClientID()
		s.ServerURL = bc.ServerURL
		s.ConnState = string(stateConnecting)
		s.ReconnectAttempt = 0
		s.Connected = false
	})

	if a.cancel != nil {
		a.cancel() // stop the old bridge's Run loop
	}
	runCtx, runCancel := context.WithCancel(context.Background())
	a.bridge, a.cancel = b, runCancel
	go b.Run(runCtx)
	return nil
}

// applyDetail folds one bridge-core StatusDetail into the UI Status.
// Callers do NOT hold a.mu; it takes the lock itself, and derives the
// six-state conn_state: 已断开 (manual) > 已在别处登录 (parked) > 已连接 >
// 重连中 > 连接中.
//
// A manual 断开 is user intent and outranks every automatic path: while
// userDisconnected is set, inbound details (a late push from the cancelled
// bridge, or the 3s poller racing the teardown) are dropped — only the
// manual Connect/SaveConfig entry points clear the flag and re-open the
// auto feeds.
func (a *App) applyDetail(d localoctop.StatusDetail) {
	a.mu.Lock()
	defer a.mu.Unlock()

	if a.userDisconnected {
		return // 用户意志：断开后不被自动状态覆盖
	}

	a.st.update(func(s *Status) {
		s.Connected = d.Connected
		s.ReconnectAttempt = d.ReconnectAttempt
		s.LastPongAt = d.LastPongAt
		if d.LastError != "" {
			s.LastError = d.LastError
		} else if d.Connected {
			s.LastError = ""
		}
		switch {
		case d.Connected:
			s.ConnState = string(stateConnected)
		case d.ReconnectAttempt > 0:
			s.ConnState = string(stateReconnecting)
		default:
			s.ConnState = string(stateConnecting)
		}
		// 0.6.1: bridge core parks on server close code 4000 (token taken
		// over by another device, no auto-reconnect). Prefer the explicit
		// contract fields from the bridge core (T2', StatusDetail.ConnState/
		// ParkReason); keep the LastError heuristic as a fallback so older
		// bridge cores still surface the state.
		if d.ConnState == localoctop.ConnStateParked &&
			(d.ParkReason == "" || d.ParkReason == localoctop.ParkReasonSuperseded) {
			s.ConnState = string(stateParked)
		} else if d.ReconnectAttempt == 0 && !d.Connected && d.LastError != "" &&
			strings.Contains(d.LastError, "4000") {
			s.ConnState = string(stateParked)
		}
	})
}

// emitStatusUnlocked pushes the current snapshot to the frontend + tray.
// The caller must NOT hold a.mu (the emit path reads ctx/snapshot under
// the lock, then emits outside it — the pre-T2 emitStatus re-locked a.mu
// from SaveConfig and deadlocked).
func (a *App) emitStatusUnlocked() {
	a.mu.Lock()
	snap := a.st.snapshot()
	a.mu.Unlock()
	emitStatus(a.ctx, snap)
}

// emitStatus is the lock-free emitter shared by all push paths. It mirrors
// the real six-state into the tray (title + tooltip) instead of the old
// binary up/down. Safe before startup wired the context (tray-only path).
func emitStatus(ctx context.Context, snap Status) {
	if ctx != nil {
		runtime.EventsEmit(ctx, "bridge:status", snap)
	}
	tray.SetStatus(localoctop.Version, snap.ConnState, snap.Connected, snap.ReconnectAttempt, snap.ClientID)
}

// emitAudit pushes one audit event to the frontend.
func (a *App) emitAudit(ev localoctop.AuditEvent) {
	a.mu.Lock()
	ctx := a.ctx
	a.mu.Unlock()
	if ctx == nil {
		return
	}
	runtime.EventsEmit(ctx, "bridge:audit", ev)
}
