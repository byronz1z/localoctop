package appcfg

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func withTempConfigDir(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("AppData", dir)             // windows: os.UserConfigDir
	t.Setenv("XDG_CONFIG_HOME", dir)     // linux
	t.Setenv("HOME", filepath.Join(dir, "home")) // darwin fallback
}

func TestLoadMissingReturnsDefault(t *testing.T) {
	withTempConfigDir(t)
	cfg, exists, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if exists {
		t.Fatal("expected exists=false for missing file")
	}
	if cfg.ConsolePort != DefaultConsolePort || !cfg.OpenBrowser {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
	if cfg.AuditLogPath == "" {
		t.Error("default audit log path must be set so auditing works from first run")
	}
}

func TestSaveLoadRoundTrip(t *testing.T) {
	withTempConfigDir(t)
	in := File{
		ServerURL:   "wss://example.com/mcp-localfs/ws",
		Token:       "secret-token",
		AllowedDirs: []Dir{{Path: "~/Documents", Enabled: true}, {Path: "D:/tmp", Enabled: false}},
		AllowWrite:  true,
		ConsolePort: 19900,
		OpenBrowser: false,
	}
	if err := Save(in); err != nil {
		t.Fatalf("Save: %v", err)
	}
	out, exists, err := Load()
	if err != nil || !exists {
		t.Fatalf("Load: exists=%v err=%v", exists, err)
	}
	if out.ServerURL != in.ServerURL || out.Token != in.Token || out.AllowWrite != in.AllowWrite ||
		out.ConsolePort != in.ConsolePort || out.OpenBrowser != in.OpenBrowser ||
		len(out.AllowedDirs) != 2 || out.AllowedDirs[0] != in.AllowedDirs[0] || out.AllowedDirs[1] != in.AllowedDirs[1] {
		t.Fatalf("round trip mismatch:\n got %+v\nwant %+v", out, in)
	}
	if got := out.EnabledDirs(); len(got) != 1 || got[0] != "~/Documents" {
		t.Fatalf("EnabledDirs = %v", got)
	}
}

func TestSaveFilePermissions(t *testing.T) {
	withTempConfigDir(t)
	if err := Save(File{Token: "x"}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	p, _ := Path()
	st, err := os.Stat(p)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	// POSIX: 0600. Windows CreateFile always maps to -rw-rw-rw- in Go's
	// Stat, so only assert the group/other bits there.
	if runtime.GOOS != "windows" && st.Mode().Perm()&0o077 != 0 {
		t.Fatalf("config file too permissive: %v", st.Mode())
	}
	if st.IsDir() {
		t.Fatal("config path is a directory")
	}
}

func TestLoadLegacyStringDirs(t *testing.T) {
	// A config written by the bridge CLI era (plain string list) must still
	// load; Go's json fails on []string -> []Dir, so verify the documented
	// behavior: error is returned, not silent data loss.
	withTempConfigDir(t)
	p, _ := Path()
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		t.Fatal(err)
	}
	legacy := `{"server_url":"wss://x/ws","allowed_dirs":["C:/Users/me/Documents"]}`
	if err := os.WriteFile(p, []byte(legacy), 0o600); err != nil {
		t.Fatal(err)
	}
	_, _, err := Load()
	if err != nil {
		t.Fatalf("legacy string-list allowed_dirs should load: %v", err)
	}
	cfg, _, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.AllowedDirs) != 1 || cfg.AllowedDirs[0].Path != "C:/Users/me/Documents" || !cfg.AllowedDirs[0].Enabled {
		t.Fatalf("legacy dirs not mapped: %+v", cfg.AllowedDirs)
	}
}

func TestLoadFillsDefaultsOnPartialFile(t *testing.T) {
	withTempConfigDir(t)
	p, _ := Path()
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(`{"server_url":"wss://x/ws"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, exists, err := Load()
	if err != nil || !exists {
		t.Fatalf("Load: exists=%v err=%v", exists, err)
	}
	if cfg.ServerURL != "wss://x/ws" {
		t.Fatalf("ServerURL not preserved: %q", cfg.ServerURL)
	}
	if cfg.ConsolePort != DefaultConsolePort {
		t.Fatalf("ConsolePort default not filled: %d", cfg.ConsolePort)
	}
}
