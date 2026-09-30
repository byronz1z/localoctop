package main

// 0.6.0 (T2) + 0.6.1 (T3') self-tests: five/six-state derivation, manual
// connect/disconnect lifecycle, the SaveConfig no-deadlock regression, and
// autostart's appcfg round-trip. Run with:
//
//	go test -run 'TestT2|TestT3|TestSettingsBindings' -v ./...
//
// The live config file is snapshotted and restored byte-for-byte around
// each test (the autostart case writes both appcfg and HKCU), so a
// developer machine keeps its real settings and registry state.

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/byronz1z/localoctop"
	"github.com/byronz1z/localoctop/internal/appcfg"
)

// snapshotConfig backs up the live config file; t.Cleanup restores it.
func snapshotConfig(t *testing.T) {
	t.Helper()
	path, err := appcfg.Path()
	if err != nil {
		t.Fatalf("appcfg.Path: %v", err)
	}
	var before []byte
	if data, err := os.ReadFile(path); err == nil {
		before = data
	}
	t.Cleanup(func() {
		if before == nil {
			_ = os.Remove(path)
		} else {
			_ = os.WriteFile(path, before, 0o600)
		}
	})
}

// snapAutostart backs up the HKCU Run value ("" = absent).
func snapAutostart(t *testing.T) {
	t.Helper()
	on, err := autostartEnabled()
	if err != nil {
		t.Skipf("autostart not probeable on this host: %v", err)
	}
	t.Cleanup(func() {
		_ = setAutostartEnabled(on)
	})
}

// TestT2ApplyDetailFiveStates walks the StatusDetail → conn_state mapping:
// every documented five-state transition, in order, against one App.
func TestT2ApplyDetailFiveStates(t *testing.T) {
	a := NewApp()

	get := func() string {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.st.snapshot().ConnState
	}

	// Fresh bridge → 连接中 (connecting)
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 0})
	if got := get(); got != string(stateConnecting) {
		t.Fatalf("fresh dial: conn_state = %q, want connecting", got)
	}

	// Online → 已连接 (connected)
	a.applyDetail(localoctop.StatusDetail{Connected: true})
	if got := get(); got != string(stateConnected) {
		t.Fatalf("online: conn_state = %q, want connected", got)
	}

	// Drop + retries → 重连中 (reconnecting), attempt count carried over
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 3, LastError: "dial tcp: refused"})
	if got := get(); got != string(stateReconnecting) {
		t.Fatalf("retrying: conn_state = %q, want reconnecting", got)
	}
	a.mu.Lock()
	snap := a.st.snapshot()
	a.mu.Unlock()
	if snap.ReconnectAttempt != 3 || !strings.Contains(snap.LastError, "refused") {
		t.Fatalf("retry detail lost: attempt=%d err=%q", snap.ReconnectAttempt, snap.LastError)
	}

	// Back online → 已连接 again, error cleared
	a.applyDetail(localoctop.StatusDetail{Connected: true, ReconnectAttempt: 0})
	if got := get(); got != string(stateConnected) {
		t.Fatalf("re-online: conn_state = %q, want connected", got)
	}
	a.mu.Lock()
	snap = a.st.snapshot()
	a.mu.Unlock()
	if snap.LastError != "" {
		t.Fatalf("online but LastError = %q, want cleared", snap.LastError)
	}

	// Manual disconnect → 已断开 (disconnected), and a LATE event push from
	// the cancelled bridge must NOT overwrite the user's chosen state.
	a.Disconnect()
	if got := get(); got != string(stateDisconnected) {
		t.Fatalf("manual disconnect: conn_state = %q, want disconnected", got)
	}
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 1})
	if got := get(); got != string(stateDisconnected) {
		t.Fatalf("late push after disconnect: conn_state = %q, want disconnected (用户意志不可被自动路径覆盖)", got)
	}
}

// TestT3ParkedState: the 0.6.1 sixth state. A takeover close (server 4000)
// shows up as a not-connected, not-retrying detail whose error names the
// code → conn_state must read parked (已在别处登录), not reconnecting —
// and the classification must survive a follow-up detail with the same
// error (the 3s poller re-pulls the parked snapshot while T2' has stopped
// the reconnect loop).
func TestT3ParkedState(t *testing.T) {
	a := NewApp()

	get := func() string {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.st.snapshot().ConnState
	}

	// Takeover detail: connected=false, attempt=0, close code in the error.
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 0,
		LastError: "websocket: close 4000 (replaced by reconnect)"})
	if got := get(); got != string(stateParked) {
		t.Fatalf("takeover 4000: conn_state = %q, want parked", got)
	}

	// The poller re-pulls the same parked snapshot: stays parked, no flapping
	// back to connecting (which would re-enable auto-reconnect visuals).
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 0,
		LastError: "websocket: close 4000 (replaced by reconnect)"})
	if got := get(); got != string(stateParked) {
		t.Fatalf("parked re-poll: conn_state = %q, want parked", got)
	}

	// Recovery is a manual act: a fresh connecting detail (Connect clicked)
	// flips the card to 连接中 — parked never overrides user intent.
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 0, LastError: ""})
	if got := get(); got != string(stateConnecting) {
		t.Fatalf("manual reconnect: conn_state = %q, want connecting", got)
	}

	// Guard: an ordinary network error with no 4000 stays reconnecting,
	// not parked (reverse case from T2' spec).
	a.applyDetail(localoctop.StatusDetail{Connected: false, ReconnectAttempt: 1,
		LastError: "dial tcp: connection refused"})
	if got := get(); got != string(stateReconnecting) {
		t.Fatalf("ordinary drop: conn_state = %q, want reconnecting", got)
	}
}

// TestT2DisconnectStaysDown: after a manual Disconnect, SaveConfig
// (user-initiated) DOES reconnect — the flag gates auto paths only, and a
// save is the user asking to connect with new settings.
func TestT2DisconnectThenSaveReconnects(t *testing.T) {
	snapshotConfig(t)
	a := NewApp()

	// Unconfigured default: SaveConfig with a valid target rebuilds.
	cfg, _, err := appcfg.Load()
	if err != nil {
		t.Fatalf("appcfg.Load: %v", err)
	}
	next := cfg
	next.ServerURL = "ws://127.0.0.1:1/ws" // unroutable: Run enters retry, fine
	next.Token = "t2-token"
	next.AllowedDirs = []appcfg.Dir{{Path: t.TempDir(), Enabled: true}}

	a.mu.Lock()
	a.cfg = next
	a.mu.Unlock()

	if err := a.SaveConfig(next); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	a.mu.Lock()
	bridgeAfterSave := a.bridge
	cancel := a.cancel
	a.mu.Unlock()
	if bridgeAfterSave == nil || cancel == nil {
		t.Fatal("SaveConfig did not build a bridge")
	}
	defer cancel()

	// Manual disconnect: bridge torn down, state 已断开.
	a.Disconnect()
	a.mu.Lock()
	bridgeAfterDisc := a.bridge
	cancelAfterDisc := a.cancel
	disconnected := a.userDisconnected
	stateAfterDisc := a.st.snapshot().ConnState
	a.mu.Unlock()
	if bridgeAfterDisc != nil || cancelAfterDisc != nil {
		t.Fatal("Disconnect left a bridge running")
	}
	if !disconnected || stateAfterDisc != string(stateDisconnected) {
		t.Fatalf("after Disconnect: userDisconnected=%v state=%q", disconnected, stateAfterDisc)
	}

	// Save again = explicit connect intent: bridge rebuilt, flag cleared.
	if err := a.SaveConfig(next); err != nil {
		t.Fatalf("SaveConfig after disconnect: %v", err)
	}
	a.mu.Lock()
	bridgeResaved := a.bridge
	cancelResaved := a.cancel
	disconnected = a.userDisconnected
	stateResaved := a.st.snapshot().ConnState
	a.mu.Unlock()
	if bridgeResaved == nil || cancelResaved == nil {
		t.Fatal("SaveConfig after Disconnect did not rebuild the bridge")
	}
	defer cancelResaved()
	if disconnected || stateResaved != string(stateConnecting) {
		t.Fatalf("resave: userDisconnected=%v state=%q, want false/connecting", disconnected, stateResaved)
	}
}

// TestT2SaveConfigNoDeadlock is the regression for the pre-T2 deadlock:
// SaveConfig held a.mu and called emitStatus, which locked a.mu again.
// With the unlocked-emit fix this must return promptly (error is fine —
// the config here is deliberately invalid).
func TestT2SaveConfigNoDeadlock(t *testing.T) {
	snapshotConfig(t)
	a := NewApp()

	bad := appcfg.File{} // no URL/token: rebuildLocked errors, bridge untouched
	done := make(chan error, 1)
	go func() { done <- a.SaveConfig(bad) }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("SaveConfig(empty) unexpectedly succeeded")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("SaveConfig deadlocked (emitStatus re-entering a.mu)")
	}
}

// TestT2AutostartRoundTrip exercises the settings-page toggle against the
// real HKCU Run key (Windows only; the registry probe skips elsewhere).
func TestT2AutostartRoundTrip(t *testing.T) {
	snapshotConfig(t)
	snapAutostart(t)

	a := NewApp()
	cfg, _, err := appcfg.Load()
	if err != nil {
		t.Fatalf("appcfg.Load: %v", err)
	}
	a.mu.Lock()
	a.cfg = cfg
	a.mu.Unlock()

	for _, want := range []bool{true, false} {
		if err := a.SetAutostart(want); err != nil {
			t.Fatalf("SetAutostart(%v): %v", want, err)
		}
		got, warn := a.GetAutostart()
		if warn != "" {
			t.Fatalf("GetAutostart warning: %s", warn)
		}
		if got != want {
			t.Fatalf("GetAutostart() = %v, want %v", got, want)
		}
		// The config mirror must agree (this is the fallback hint for the
		// settings page when the registry is unreadable).
		onDisk, _, err := appcfg.Load()
		if err != nil {
			t.Fatalf("appcfg.Load: %v", err)
		}
		if onDisk.Autostart == nil || *onDisk.Autostart != want {
			t.Fatalf("file autostart = %v, want %v", onDisk.Autostart, want)
		}
	}

	// The Run value's shape: quoted exe path + --minimized (only when on).
	if err := a.SetAutostart(true); err != nil {
		t.Fatalf("SetAutostart(true): %v", err)
	}
	cmd := autostartCommand(mustExe(t))
	if !strings.HasPrefix(cmd, `"`) || !strings.HasSuffix(cmd, `" --minimized`) {
		t.Fatalf("autostart command shape wrong: %q", cmd)
	}
}

func mustExe(t *testing.T) string {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatalf("os.Executable: %v", err)
	}
	return exe
}
