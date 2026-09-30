package main

// M3 self-test: exercise the newly bound settings methods against the real
// appcfg file, mirroring what the settings page's loadSettingsPage() does
// through the JS bindings. Run with:
//
//	go test -run TestSettingsBindings -v ./...
//
// The live config file is snapshotted and restored byte-for-byte around the
// test (SetConfirmExit persists through appcfg.Save), so a developer machine
// keeps its real settings.

import (
	"os"
	"testing"

	"github.com/byronz1z/localoctop/internal/appcfg"
)

func TestSettingsBindings(t *testing.T) {
	// Snapshot the live file (may not exist on a fresh machine).
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

	a := NewApp()

	// Mirror startup's load so a.cfg reflects the file on disk.
	cfg, _, err := appcfg.Load()
	if err != nil {
		t.Fatalf("appcfg.Load: %v", err)
	}
	a.mu.Lock()
	a.cfg = cfg
	a.mu.Unlock()

	// GetConfirmExit / SetConfirmExit round-trip, both polarities.
	orig := a.GetConfirmExit()
	for _, want := range []bool{false, true, orig} {
		if err := a.SetConfirmExit(want); err != nil {
			t.Fatalf("SetConfirmExit(%v): %v", want, err)
		}
		if got := a.GetConfirmExit(); got != want {
			t.Fatalf("GetConfirmExit() = %v, want %v", got, want)
		}
		// The file must agree (this is what the next app start reads).
		onDisk, _, err := appcfg.Load()
		if err != nil {
			t.Fatalf("appcfg.Load after SetConfirmExit: %v", err)
		}
		if got := onDisk.ConfirmExit != nil && *onDisk.ConfirmExit; got != want {
			t.Fatalf("file confirm_exit = %v, want %v", got, want)
		}
	}

	// GetSettingsInfo: version + paths must be the real ones.
	info := a.GetSettingsInfo()
	if info.Version == "" {
		t.Fatal("GetSettingsInfo().Version empty")
	}
	if _, err := os.Stat(info.ConfigPath); err != nil {
		t.Errorf("ConfigPath %q not statable: %v", info.ConfigPath, err)
	}
	if info.AuditLogPath == "" {
		t.Fatal("GetSettingsInfo().AuditLogPath empty")
	}
	t.Logf("version=%s config=%s audit=%s", info.Version, info.ConfigPath, info.AuditLogPath)
}
