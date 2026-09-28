package octobridge

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// AuditEvent is one JSON line in the local audit log. Every tool invocation —
// allowed or blocked — produces exactly one event, which is the employee-side
// record demanded by the task spec ("本地审计日志").
type AuditEvent struct {
	Time     string         `json:"ts"`
	Method   string         `json:"method"`
	Path     string         `json:"path,omitempty"`
	Decision string         `json:"decision"` // "allow" | "deny" | "error"
	Code     int            `json:"code"`
	Detail   string         `json:"detail,omitempty"`
	Duration string         `json:"duration,omitempty"`
	Size     int64          `json:"size,omitempty"`
	ClientID string         `json:"client_id,omitempty"`
	Extra    map[string]any `json:"extra,omitempty"`
}

// AuditLogger appends audit events as JSON lines. It is safe for concurrent
// use and rotates the file (path -> path.1) once it exceeds maxBytes.
type AuditLogger struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	f        *os.File
	log      Logger
	hook     func(AuditEvent)
	now      func() time.Time // overridable in tests
}

// NewAuditLogger opens (creating parents as needed) the audit log at path.
// An empty path returns a logger that only dispatches to hook (which may be
// nil), i.e. file auditing disabled.
func NewAuditLogger(path string, maxBytes int64, log Logger, hook func(AuditEvent)) (*AuditLogger, error) {
	if log == nil {
		log = NopLogger{}
	}
	a := &AuditLogger{path: path, maxBytes: maxBytes, log: log, hook: hook, now: time.Now}
	if path == "" {
		return a, nil
	}
	if err := a.open(); err != nil {
		return nil, err
	}
	return a, nil
}

func (a *AuditLogger) open() error {
	if dir := filepath.Dir(a.path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("audit: create dir %s: %w", dir, err)
		}
	}
	f, err := os.OpenFile(a.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("audit: open %s: %w", a.path, err)
	}
	a.f = f
	return nil
}

// Record writes one event. Failures are logged, never returned: auditing must
// not break tool serving, but every failure is visible in the client log.
func (a *AuditLogger) Record(ev AuditEvent) {
	if ev.Time == "" {
		ev.Time = a.now().UTC().Format(time.RFC3339Nano)
	}
	if a.hook != nil {
		a.hook(ev)
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	if a.f == nil {
		return
	}
	if a.maxBytes > 0 {
		if st, err := a.f.Stat(); err == nil && st.Size() >= a.maxBytes {
			a.rotateLocked()
		}
	}
	line, err := json.Marshal(ev)
	if err != nil {
		a.log.Errorf("audit: marshal: %v", err)
		return
	}
	if _, err := a.f.Write(append(line, '\n')); err != nil {
		a.log.Errorf("audit: write: %v", err)
	}
}

// rotateLocked renames path -> path.1 (discarding any previous .1) and
// reopens a fresh file. Best-effort: on error the old handle stays open.
func (a *AuditLogger) rotateLocked() {
	_ = a.f.Close()
	a.f = nil
	rotated := a.path + ".1"
	_ = os.Remove(rotated)
	if err := os.Rename(a.path, rotated); err != nil {
		a.log.Warnf("audit: rotate rename: %v", err)
	}
	if err := a.open(); err != nil {
		a.log.Errorf("audit: reopen after rotate: %v", err)
	} else {
		a.log.Infof("audit log rotated to %s", rotated)
	}
}

// Close flushes and closes the audit file, if any.
func (a *AuditLogger) Close() error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.f == nil {
		return nil
	}
	err := a.f.Close()
	a.f = nil
	return err
}
