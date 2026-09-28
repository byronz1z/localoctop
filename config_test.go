package octobridge

import (
	"strings"
	"testing"
)

func TestConfigValidate_RequiresServerURL(t *testing.T) {
	c := NewConfig()
	c.Token = "t"
	c.AllowedDirs = []string{"."}
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "ServerURL") {
		t.Fatalf("expected ServerURL error, got %v", err)
	}
}

func TestConfigValidate_RequiresToken(t *testing.T) {
	c := NewConfig()
	c.ServerURL = "wss://example.com/ws"
	c.AllowedDirs = []string{"."}
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "Token") {
		t.Fatalf("expected Token error, got %v", err)
	}
}

func TestConfigValidate_RequiresAllowedDirs(t *testing.T) {
	c := NewConfig()
	c.ServerURL = "wss://example.com/ws"
	c.Token = "t"
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "AllowedDirs") {
		t.Fatalf("expected AllowedDirs error, got %v", err)
	}
}

func TestConfigValidate_NormalizesHTTPScheme(t *testing.T) {
	cases := map[string]string{
		"https://h/ws": "wss://h/ws",
		"http://h/ws":  "ws://h/ws",
		"ws://h/ws":   "ws://h/ws",
		"wss://h/ws":  "wss://h/ws",
	}
	for in, want := range cases {
		got, err := normalizeWSScheme(in)
		if err != nil {
			t.Fatalf("%s: unexpected error %v", in, err)
		}
		if got != want {
			t.Fatalf("%s: got %s want %s", in, got, want)
		}
	}
	// Validate() must store the normalized scheme back onto the config.
	c := NewConfig()
	c.ServerURL = "https://h/ws"
	c.Token = "t"
	c.AllowedDirs = []string{t.TempDir()}
	if err := c.Validate(); err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if c.ServerURL != "wss://h/ws" {
		t.Fatalf("ServerURL not normalized in place: %s", c.ServerURL)
	}
}

func TestConfigValidate_RejectsBadScheme(t *testing.T) {
	if _, err := normalizeWSScheme("ftp://h/ws"); err == nil {
		t.Fatal("expected ftp scheme to be rejected")
	}
	if _, err := normalizeWSScheme("h/ws"); err == nil {
		t.Fatal("expected schemeless URL to be rejected")
	}
}

func TestConfigValidate_AbsolutizesAndDedupesRoots(t *testing.T) {
	dir := t.TempDir()
	c := NewConfig()
	c.ServerURL = "wss://example.com/ws"
	c.Token = "t"
	c.AllowedDirs = []string{dir, dir, dir + string('/') + ".." + string('/') + base(dir)}
	if err := c.Validate(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(c.AllowedDirs) != 1 {
		t.Fatalf("expected dedupe to 1 root, got %v", c.AllowedDirs)
	}
	if c.AllowedDirs[0] != dir {
		t.Fatalf("expected absolute root %s, got %s", dir, c.AllowedDirs[0])
	}
}

func TestConfigValidate_DefaultsApplied(t *testing.T) {
	c := NewConfig()
	c.ServerURL = "wss://example.com/ws"
	c.Token = "t"
	c.AllowedDirs = []string{t.TempDir()}
	c.RequestWait = 0
	c.MinBackoff = 0
	if err := c.Validate(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if c.RequestWait != DefaultRequestWait || c.MinBackoff != DefaultMinBackoff {
		t.Fatalf("defaults not applied: %+v", c)
	}
	if c.MaxReadBytes != DefaultMaxReadBytes || c.MaxWriteBytes != DefaultMaxWriteBytes {
		t.Fatalf("size defaults wrong: read=%d write=%d", c.MaxReadBytes, c.MaxWriteBytes)
	}
}

func TestExpandPath_Tilde(t *testing.T) {
	got, err := expandPath("~/docs")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.HasPrefix(got, "~") {
		t.Fatalf("tilde not expanded: %s", got)
	}
	if _, err := expandPath(""); err == nil {
		t.Fatal("expected empty path error")
	}
}

func base(p string) string {
	i := strings.LastIndexAny(p, `/\`)
	if i < 0 {
		return p
	}
	return p[i+1:]
}
