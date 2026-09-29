package localoctop

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"time"
)

const (
	// DefaultMaxReadBytes caps a single read_file result (task spec: 20 MB).
	DefaultMaxReadBytes = 20 << 20
	// DefaultMaxWriteBytes caps a single write_file payload (task spec: 10 MB).
	DefaultMaxWriteBytes = 10 << 20
	// DefaultMaxMediaBytes caps one-shot binary transfers (read_media_file /
	// zip_files). v0.5.0 ruling: text stays windowed at MaxReadBytes (context
	// limits), binaries get a generous single-shot cap because multimodal
	// consumers use them directly. 30 MB raw -> ~40 MB base64 -> one ~41 MB
	// JSON frame, inside the 48 MiB frame ceiling below.
	DefaultMaxMediaBytes = 30 << 20

	// Default reconnect backoff ladder: 1s, 2s, 4s ... capped at 60s.
	DefaultMinBackoff    = 1 * time.Second
	DefaultMaxBackoff    = 60 * time.Second
	DefaultRequestWait   = 30 * time.Second
	DefaultDialTimeout   = 10 * time.Second
	DefaultPingInterval  = 25 * time.Second
	DefaultPongWait      = 60 * time.Second
	DefaultMaxMsgBytes   = 48 << 20 // inbound frame ceiling: fits a 30 MB media/zip frame base64'd + JSON overhead.
	DefaultAuditMaxBytes = 8 << 20  // per-file audit log size before rotation.
)

// Config describes how the bridge client connects and what it is allowed to do.
//
// Use NewConfig to get sensible defaults, then override fields as needed.
type Config struct {
	// ServerURL is the bridge WebSocket endpoint, e.g.
	// "wss://example.com/mcp/localoctop/ws". http(s) schemes are accepted and
	// translated to ws(s).
	ServerURL string

	// Token authenticates this client to the adapter (and binds it to a user).
	Token string

	// AllowedDirs are the only directories the bridge will serve. Every path
	// is resolved and must live inside one of these roots. At least one is
	// required. Entries may be relative (resolved against the working dir) or
	// use ~ for the home directory.
	AllowedDirs []string

	// AllowWrite enables the write-class tools (write/create/edit/move/delete/
	// remove/zip/unzip). Library zero-value is false (conservative for
	// embedders); the product default is ON — the desktop shell seeds it from
	// appcfg (AllowWrite=true default) and headless from LOCALOCTOP_ALLOW_WRITE
	// (user ruling 2026-09-29: switch kept, default on).
	AllowWrite bool

	// MaxReadBytes / MaxWriteBytes bound single-operation payload sizes.
	MaxReadBytes  int64
	MaxWriteBytes int64
	// MaxMediaBytes bounds one-shot binary transfers (read_media_file,
	// zip_files archive). v0.5.0.
	MaxMediaBytes int64

	// AuditLogPath is where JSON-lines audit records are appended. Empty
	// disables file auditing (events still reach OnAudit / StderrLogger).
	AuditLogPath string
	// AuditMaxBytes rotates the audit file once it exceeds this size.
	AuditMaxBytes int64

	// RequestWait is how long a single tool call may take before timing out.
	RequestWait time.Duration
	// DialTimeout bounds each WebSocket connect attempt.
	DialTimeout time.Duration
	// MinBackoff / MaxBackoff define the exponential reconnect ladder.
	MinBackoff time.Duration
	MaxBackoff time.Duration
	// PingInterval / PongWait drive the WebSocket keep-alive.
	PingInterval time.Duration
	PongWait     time.Duration
	// MaxMsgBytes caps inbound frame size (protocol-level guard).
	MaxMsgBytes int64

	// Logger, when set, receives human-readable log lines. Defaults to a
	// stderr logger inside New.
	Logger Logger

	// OnStatus, when set, is called on connect/disconnect transitions.
	OnStatus func(connected bool, err error)
	// OnAudit, when set, receives every audit event in addition to the file.
	OnAudit func(AuditEvent)
}

// NewConfig returns a Config populated with the documented defaults.
func NewConfig() Config {
	return Config{
		MaxReadBytes:  DefaultMaxReadBytes,
		MaxWriteBytes: DefaultMaxWriteBytes,
		MaxMediaBytes: DefaultMaxMediaBytes,
		RequestWait:   DefaultRequestWait,
		DialTimeout:   DefaultDialTimeout,
		MinBackoff:    DefaultMinBackoff,
		MaxBackoff:    DefaultMaxBackoff,
		PingInterval:  DefaultPingInterval,
		PongWait:      DefaultPongWait,
		MaxMsgBytes:   DefaultMaxMsgBytes,
		AuditMaxBytes: DefaultAuditMaxBytes,
	}
}

// Validate checks the configuration and normalizes derived fields. AllowedDirs
// entries are expanded (absolute, ~ resolved) in place so the rest of the
// package can compare against clean roots.
func (c *Config) Validate() error {
	if c.ServerURL == "" {
		return errors.New("config: ServerURL is required")
	}
	normalized, err := normalizeWSScheme(c.ServerURL)
	if err != nil {
		return fmt.Errorf("config: invalid ServerURL %q: %w", c.ServerURL, err)
	}
	c.ServerURL = normalized
	if c.Token == "" {
		return errors.New("config: Token is required")
	}
	if len(c.AllowedDirs) == 0 {
		return errors.New("config: at least one AllowedDirs entry is required")
	}
	if c.MaxReadBytes <= 0 {
		c.MaxReadBytes = DefaultMaxReadBytes
	}
	if c.MaxWriteBytes <= 0 {
		c.MaxWriteBytes = DefaultMaxWriteBytes
	}
	if c.MaxMediaBytes <= 0 {
		c.MaxMediaBytes = DefaultMaxMediaBytes
	}
	if c.RequestWait <= 0 {
		c.RequestWait = DefaultRequestWait
	}
	if c.DialTimeout <= 0 {
		c.DialTimeout = DefaultDialTimeout
	}
	if c.MinBackoff <= 0 {
		c.MinBackoff = DefaultMinBackoff
	}
	if c.MaxBackoff < c.MinBackoff {
		c.MaxBackoff = DefaultMaxBackoff
	}
	if c.PingInterval <= 0 {
		c.PingInterval = DefaultPingInterval
	}
	if c.PongWait <= 0 {
		c.PongWait = DefaultPongWait
	}
	if c.MaxMsgBytes <= 0 {
		c.MaxMsgBytes = DefaultMaxMsgBytes
	}
	if c.AuditMaxBytes <= 0 {
		c.AuditMaxBytes = DefaultAuditMaxBytes
	}

	expanded := make([]string, 0, len(c.AllowedDirs))
	seen := make(map[string]struct{}, len(c.AllowedDirs))
	for _, raw := range c.AllowedDirs {
		p, err := expandPath(raw)
		if err != nil {
			return fmt.Errorf("config: invalid allowed dir %q: %w", raw, err)
		}
		abs, err := filepath.Abs(p)
		if err != nil {
			return fmt.Errorf("config: cannot resolve allowed dir %q: %w", raw, err)
		}
		// Normalize roots to their fully-resolved form. Handlers receive
		// symlink-resolved paths from the guard (ensureInside); if the root
		// kept its junction form, relDisplay and samePath comparisons would
		// mismatch (e.g. Windows runner TEMP is a junction, employee dirs
		// may live behind mount points). Best-effort: non-existent roots
		// keep their lexical form until they appear.
		if real, rerr := evalSymlinksBestEffort(abs); rerr == nil {
			abs = real
		}
		if _, dup := seen[abs]; dup {
			continue
		}
		seen[abs] = struct{}{}
		expanded = append(expanded, abs)
	}
	c.AllowedDirs = expanded
	return nil
}

// normalizeWSScheme rewrites http->ws and https->wss, leaving ws/wss intact.
func normalizeWSScheme(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "ws", "wss":
		// already fine
	case "http":
		u.Scheme = "ws"
	case "https":
		u.Scheme = "wss"
	case "":
		return "", errors.New("missing scheme (want ws:// or wss://)")
	default:
		return "", fmt.Errorf("unsupported scheme %q", u.Scheme)
	}
	if u.Host == "" {
		return "", errors.New("missing host")
	}
	return u.String(), nil
}

// expandPath resolves a leading ~ to the user's home directory.
func expandPath(p string) (string, error) {
	if p == "" {
		return "", errors.New("empty path")
	}
	if p == "~" {
		return os.UserHomeDir()
	}
	if len(p) >= 2 && p[0] == '~' && (p[1] == '/' || p[1] == '\\') {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, p[2:]), nil
	}
	return p, nil
}
