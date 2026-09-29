package localoctop

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestAuditLogger_WritesJSONLines(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit", "audit.log")
	a, err := NewAuditLogger(path, 1<<20, NopLogger{}, nil)
	if err != nil {
		t.Fatalf("NewAuditLogger: %v", err)
	}
	a.Record(AuditEvent{Method: "read_file", Path: "a.txt", Decision: "allow", Code: CodeOK})
	a.Record(AuditEvent{Method: "read_file", Path: "../evil", Decision: "deny", Code: CodeNotAllowed})
	if err := a.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read audit log: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 lines, got %d: %q", len(lines), data)
	}
	var ev AuditEvent
	if err := json.Unmarshal([]byte(lines[1]), &ev); err != nil {
		t.Fatalf("line is not valid JSON: %v", err)
	}
	if ev.Decision != "deny" || ev.Code != CodeNotAllowed || ev.Time == "" {
		t.Fatalf("unexpected audit event: %+v", ev)
	}
}

func TestAuditLogger_HookAndConcurrency(t *testing.T) {
	var mu sync.Mutex
	var events []AuditEvent
	a, err := NewAuditLogger("", 0, NopLogger{}, func(ev AuditEvent) {
		mu.Lock()
		events = append(events, ev)
		mu.Unlock()
	})
	if err != nil {
		t.Fatalf("NewAuditLogger: %v", err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			a.Record(AuditEvent{Method: "list_directory", Decision: "allow"})
		}(i)
	}
	wg.Wait()
	mu.Lock()
	defer mu.Unlock()
	if len(events) != 50 {
		t.Fatalf("expected 50 hook events, got %d", len(events))
	}
}

func TestAuditLogger_Rotates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.log")
	// maxBytes=1 forces rotation on every second write.
	a, err := NewAuditLogger(path, 1, NopLogger{}, nil)
	if err != nil {
		t.Fatalf("NewAuditLogger: %v", err)
	}
	for i := 0; i < 5; i++ {
		a.Record(AuditEvent{Method: "read_file", Decision: "allow"})
	}
	if err := a.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := os.Stat(path + ".1"); err != nil {
		t.Fatalf("expected rotated file %s.1: %v", path, err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	// After rotation the active file holds only the most recent line.
	if n := len(strings.Split(strings.TrimSpace(string(data)), "\n")); n != 1 {
		t.Fatalf("expected 1 line after rotation, got %d", n)
	}
}

func TestAuditLogger_CloseTwiceSafe(t *testing.T) {
	a, err := NewAuditLogger(filepath.Join(t.TempDir(), "a.log"), 0, NopLogger{}, nil)
	if err != nil {
		t.Fatalf("NewAuditLogger: %v", err)
	}
	if err := a.Close(); err != nil {
		t.Fatalf("first close: %v", err)
	}
	if err := a.Close(); err != nil {
		t.Fatalf("second close should be a no-op: %v", err)
	}
}

func TestAuditEvent_TimestampFormat(t *testing.T) {
	a, err := NewAuditLogger("", 0, NopLogger{}, nil)
	if err != nil {
		t.Fatalf("NewAuditLogger: %v", err)
	}
	a.now = func() time.Time { return time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC) }
	var captured AuditEvent
	a.hook = func(ev AuditEvent) { captured = ev }
	a.Record(AuditEvent{Method: "get_file_info", Decision: "allow"})
	if captured.Time != "2026-09-28T12:00:00Z" {
		t.Fatalf("unexpected timestamp %q", captured.Time)
	}
}
