// Package appcfg persists the desktop shell's user-editable settings as JSON
// in the OS config directory. It is deliberately separate from the bridge
// core's Config (an in-memory struct with its own validation): the shell loads
// AppConfig, maps it into localoctop.Config, and rebuilds the Bridge whenever
// settings change. Tokens are written to disk only — never logged.
package appcfg

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// DefaultConsolePort is the preferred console port; on conflict the shell
// probes upward (see console.Listen) up to ConsolePortRange.
const (
	DefaultConsolePort = 19880
	ConsolePortRange   = 20
)

// Dir is one whitelist entry. Enabled=false keeps the entry on disk (so the
// console can re-enable it) but excludes it from the bridge's roots: a
// disabled directory is not served.
type Dir struct {
	Path    string `json:"path"`
	Enabled bool   `json:"enabled"`
}

// RecentServer is one entry of the remembered server list: the URL plus the
// token that was last used with it. The token stays on disk in the 0600
// config file and is only ever sent back to the console page on loopback —
// it is never auto-filled from the network.
type RecentServer struct {
	URL   string `json:"url"`
	Token string `json:"token"`
}

// MaxRecentServers caps the remembered server list (task spec: 5).
const MaxRecentServers = 5

// File is everything the local web console can read and write.
type File struct {
	ServerURL   string `json:"server_url"`
	Token       string `json:"token"`
	AllowedDirs []Dir  `json:"allowed_dirs"`
	AllowWrite  bool   `json:"allow_write"`
	ConsolePort int    `json:"console_port"` // 0 = default (19880)
	OpenBrowser bool   `json:"open_browser"` // open the console in the browser on start

	// RecentServers remembers the last used server connections (deduped,
	// most recent first, capped at MaxRecentServers) so the console can
	// offer them in a dropdown. Absent in pre-0.2.1 files: loads as empty.
	RecentServers []RecentServer `json:"recent_servers,omitempty"`

	// AuditLogPath is fixed at the data dir by DefaultPath; kept in the file
	// so power users can redirect it.
	AuditLogPath string `json:"audit_log_path"`
}

// Default returns the zero-settings file: empty server/token/dirs (triggers
// first-run onboarding in the console), write access ON by default (user
// ruling 2026-09-29: switch kept, default checked; untick = read-only),
// console on the default port, browser auto-open on, and the audit log at
// its default location so auditing is on from the very first run.
func Default() File {
	f := File{
		ConsolePort: DefaultConsolePort,
		OpenBrowser: true,
		AllowWrite:  true,
	}
	if p, err := AuditPath(); err == nil {
		f.AuditLogPath = p
	}
	return f
}

// EnabledDirs returns the paths of enabled entries, suitable for
// localoctop.Config.AllowedDirs.
func (f File) EnabledDirs() []string {
	var out []string
	for _, d := range f.AllowedDirs {
		if d.Enabled && d.Path != "" {
			out = append(out, d.Path)
		}
	}
	return out
}

// RememberServer records a used server connection in RecentServers: the URL
// is deduped case-insensitively, the entry moves to the front, and the list
// is capped at MaxRecentServers. Empty URLs are ignored.
func (f *File) RememberServer(url, token string) {
	url = strings.TrimSpace(url)
	if url == "" {
		return
	}
	out := []RecentServer{{URL: url, Token: token}}
	for _, r := range f.RecentServers {
		if !strings.EqualFold(r.URL, url) {
			out = append(out, r)
		}
	}
	if len(out) > MaxRecentServers {
		out = out[:MaxRecentServers]
	}
	f.RecentServers = out
}

// UnmarshalJSON accepts both the current form ([]Dir) and the legacy plain
// string list (["C:/dir", ...] as the bridge CLI consumed from flags), so a
// config hand-written against the old shape still loads with all entries
// enabled.
func (d *Dir) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err == nil {
		d.Path, d.Enabled = s, true
		return nil
	}
	type dir Dir // avoid recursion into this method
	var v dir
	if err := json.Unmarshal(data, &v); err != nil {
		return err
	}
	*d = Dir(v)
	return nil
}

// Path returns the config file location:
//
//	Windows: %AppData%\localoctop\config.json
//	Linux:   $XDG_CONFIG_HOME/localoctop/config.json
//	macOS:   $HOME/Library/Application Support/localoctop/config.json
func Path() (string, error) {
	base, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("appcfg: locate config dir: %w", err)
	}
	return filepath.Join(base, "localoctop", "config.json"), nil
}

// AuditPath returns the default audit log location in the same base dir.
func AuditPath() (string, error) {
	base, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("appcfg: locate data dir: %w", err)
	}
	return filepath.Join(base, "localoctop", "audit.jsonl"), nil
}

// Load reads the config file. A missing file is not an error: it returns
// Default() and exists=false — the console's first-run flow starts from there.
// A legacy plain-string allowed_dirs list (from bridge CLI setups) is
// tolerated and mapped to enabled entries.
func Load() (File, bool, error) {
	p, err := Path()
	if err != nil {
		return Default(), false, err
	}
	data, err := os.ReadFile(p)
	if os.IsNotExist(err) {
		return Default(), false, nil
	}
	if err != nil {
		return Default(), false, fmt.Errorf("appcfg: read %s: %w", p, err)
	}
	cfg := Default()
	if err := json.Unmarshal(data, &cfg); err != nil {
		return Default(), false, fmt.Errorf("appcfg: parse %s: %w", p, err)
	}
	if cfg.ConsolePort <= 0 {
		cfg.ConsolePort = DefaultConsolePort
	}
	if cfg.AuditLogPath == "" {
		if ap, err := AuditPath(); err == nil {
			cfg.AuditLogPath = ap
		}
	}
	return cfg, true, nil
}

// Save writes the config file atomically (tmp + rename), creating the
// directory as needed. The file is 0600: it contains the bridge token.
func Save(cfg File) error {
	p, err := Path()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return fmt.Errorf("appcfg: create dir: %w", err)
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("appcfg: marshal: %w", err)
	}
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("appcfg: write %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, p); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("appcfg: rename %s: %w", p, err)
	}
	return nil
}
