package octobridge

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// mockAdapter is a minimal in-process stand-in for the octop-local-bridge adapter's
// WS endpoint: it accepts a token, reads the register frame, and lets the
// test push tool calls and read responses.
type mockAdapter struct {
	t     *testing.T
	token string
	srv   *httptest.Server

	mu       sync.Mutex
	register chan RegisterFrame
	conns    chan *WSConn
}

func newMockAdapter(t *testing.T, token string) *mockAdapter {
	t.Helper()
	m := &mockAdapter{
		t:        t,
		token:    token,
		register: make(chan RegisterFrame, 8),
		conns:    make(chan *WSConn, 8),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/mcp-localfs/ws", func(w http.ResponseWriter, r *http.Request) {
		// The bridge sends its token as Authorization: Bearer (see
		// Bridge.runOnce), so the mock must validate the same header the
		// real adapter does — checking the query string instead let a
		// wrong-token client through whenever the URL carried the good one.
		if got := r.Header.Get("Authorization"); got != "Bearer "+token {
			http.Error(w, "bad token", http.StatusUnauthorized)
			return
		}
		conn, err := UpgradeWS(w, r, DefaultMaxMsgBytes)
		if err != nil {
			return
		}
		_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
		raw, err := conn.ReadMessage()
		if err != nil {
			conn.Close()
			return
		}
		var reg RegisterFrame
		if err := json.Unmarshal(raw, &reg); err != nil || reg.Type != "register" {
			conn.Close()
			return
		}
		_ = conn.SetReadDeadline(time.Time{})
		select {
		case m.register <- reg:
		case <-time.After(time.Second):
		}
		select {
		case m.conns <- conn:
		default:
		}
		// The connection was hijacked, so it stays open after this handler
		// returns. The test becomes the sole reader via mock.call; running a
		// pump here as well would race it for response frames.
	})
	m.srv = httptest.NewServer(mux)
	t.Cleanup(func() { m.srv.Close() })
	return m
}

func (m *mockAdapter) wsURL() string {
	return "ws" + strings.TrimPrefix(m.srv.URL, "http") + "/mcp-localfs/ws?token=" + m.token
}

// call pushes a tool call to the connected bridge and waits for the response.
func (m *mockAdapter) call(t *testing.T, conn *WSConn, id int, method string, params map[string]any) Response {
	t.Helper()
	frame, _ := json.Marshal(map[string]any{"id": id, "method": method, "params": params})
	if err := conn.WriteMessage(frame); err != nil {
		t.Fatalf("mock write: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	for {
		raw, err := conn.ReadMessage()
		if err != nil {
			t.Fatalf("mock read response: %v", err)
		}
		var resp Response
		if err := json.Unmarshal(raw, &resp); err != nil {
			continue // ignore non-response frames
		}
		return resp
	}
}

func newTestBridge(t *testing.T, url, token string, allowWrite bool) *Bridge {
	t.Helper()
	root := t.TempDir()
	must(t, os.WriteFile(filepath.Join(root, "hello.txt"), []byte("hi there\n"), 0o644))
	must(t, os.MkdirAll(filepath.Join(root, "sub"), 0o755))

	cfg := NewConfig()
	cfg.ServerURL = url
	cfg.Token = token
	cfg.AllowedDirs = []string{root}
	cfg.AllowWrite = allowWrite
	cfg.Logger = NopLogger{}
	cfg.AuditLogPath = filepath.Join(root, "audit.log")
	cfg.MinBackoff = 50 * time.Millisecond // keep reconnect tests fast
	cfg.MaxBackoff = 200 * time.Millisecond
	cfg.PingInterval = time.Second
	cfg.PongWait = 5 * time.Second
	b, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(b.Shutdown)
	return b
}

// TestE2E_RegisterAndToolCall covers the happy path over a real WebSocket:
// handshake, register frame, list_directory + read_file responses.
func TestE2E_RegisterAndToolCall(t *testing.T) {
	mock := newMockAdapter(t, "tok-123")
	b := newTestBridge(t, mock.wsURL(), "tok-123", false)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	var reg RegisterFrame
	select {
	case reg = <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register frame")
	}
	if reg.Token != "tok-123" || reg.ClientID == "" || reg.WriteMode {
		t.Fatalf("unexpected register frame: %+v", reg)
	}

	conn := <-mock.conns

	resp := mock.call(t, conn, 1, MethodListDirectory, map[string]any{"path": "."})
	if resp.Error != nil {
		t.Fatalf("list_directory errored: %+v", resp.Error)
	}
	raw, _ := json.Marshal(resp.Result)
	if !strings.Contains(string(raw), "hello.txt") {
		t.Fatalf("hello.txt missing from listing: %s", raw)
	}

	resp = mock.call(t, conn, 2, MethodReadFile, map[string]any{"path": "hello.txt"})
	if resp.Error != nil {
		t.Fatalf("read_file errored: %+v", resp.Error)
	}
	raw, _ = json.Marshal(resp.Result)
	if !strings.Contains(string(raw), "hi there") {
		t.Fatalf("content missing from read result: %s", raw)
	}
}

// TestE2E_TraversalDeniedEndToEnd proves the denial travels the full wire
// protocol with the CodeNotAllowed error.
func TestE2E_TraversalDeniedEndToEnd(t *testing.T) {
	mock := newMockAdapter(t, "tok-sec")
	b := newTestBridge(t, mock.wsURL(), "tok-sec", false)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	conn := <-mock.conns

	resp := mock.call(t, conn, 7, MethodReadFile, map[string]any{"path": "../../../etc/passwd"})
	if resp.Error == nil {
		t.Fatalf("expected error for traversal path, got result %+v", resp.Result)
	}
	if resp.Error.Code != CodeNotAllowed {
		t.Fatalf("expected CodeNotAllowed (%d), got %d: %s", CodeNotAllowed, resp.Error.Code, resp.Error.Message)
	}

	// Write must also be refused while AllowWrite=false.
	resp = mock.call(t, conn, 8, MethodWriteFile, map[string]any{"path": "x.txt", "content": "no"})
	if resp.Error == nil || resp.Error.Code != CodeWriteDisabled {
		t.Fatalf("expected CodeWriteDisabled, got %+v", resp.Error)
	}

	// Unknown methods get CodeMethodNotFound.
	resp = mock.call(t, conn, 9, "delete_everything", nil)
	if resp.Error == nil || resp.Error.Code != CodeMethodNotFound {
		t.Fatalf("expected CodeMethodNotFound, got %+v", resp.Error)
	}
}

// TestE2E_AuditLogRecorded checks that served calls land in the audit file.
func TestE2E_AuditLogRecorded(t *testing.T) {
	mock := newMockAdapter(t, "tok-audit")
	b := newTestBridge(t, mock.wsURL(), "tok-audit", false)
	auditPath := b.cfg.AuditLogPath

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()
	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	conn := <-mock.conns

	mock.call(t, conn, 1, MethodGetFileInfo, map[string]any{"path": "hello.txt"})
	mock.call(t, conn, 2, MethodReadFile, map[string]any{"path": "../evil"})

	// Give the audit goroutines a moment to flush.
	deadline := time.Now().Add(3 * time.Second)
	var data []byte
	for time.Now().Before(deadline) {
		data, _ = os.ReadFile(auditPath)
		if strings.Count(string(data), "\n") >= 2 {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) < 2 {
		t.Fatalf("expected >=2 audit lines, got %d: %q", len(lines), data)
	}
	if !strings.Contains(lines[1], `"decision":"deny"`) {
		t.Fatalf("expected deny decision in second audit line: %s", lines[1])
	}
}

// TestE2E_ReconnectAfterServerClose verifies the exponential-backoff loop
// re-dials and re-registers after the adapter drops the session.
func TestE2E_ReconnectAfterServerClose(t *testing.T) {
	mock := newMockAdapter(t, "tok-re")
	b := newTestBridge(t, mock.wsURL(), "tok-re", false)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	// First registration.
	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for first register")
	}
	first := <-mock.conns

	// Adapter drops the connection.
	first.Close()

	// The bridge must reconnect and re-register (backoff is 50ms in tests).
	var reg RegisterFrame
	select {
	case reg = <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("bridge did not re-register after disconnect")
	}
	second := <-mock.conns
	if second == first {
		t.Fatal("expected a fresh connection object")
	}
	_ = reg

	// And the new session still serves calls.
	resp := mock.call(t, second, 42, MethodReadFile, map[string]any{"path": "hello.txt"})
	if resp.Error != nil {
		t.Fatalf("read_file after reconnect errored: %+v", resp.Error)
	}
}

// TestE2E_BadTokenRejected ensures the handshake fails cleanly when the
// adapter rejects the token, and the bridge keeps retrying without crashing.
func TestE2E_BadTokenRejected(t *testing.T) {
	mock := newMockAdapter(t, "good-token")
	b := newTestBridge(t, mock.wsURL(), "wrong-token", false)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() {
		_ = b.Run(ctx)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(4 * time.Second):
		t.Fatal("Run did not return after context timeout")
	}
	select {
	case <-mock.register:
		t.Fatal("bad token must never register")
	default:
	}
	if b.Connected() {
		t.Fatal("bridge must not report connected with a bad token")
	}
}

// TestHandleRequest_Direct exercises the transport-agnostic entry point used
// by embedders that drive the bridge in-process without a socket.
func TestHandleRequest_Direct(t *testing.T) {
	root := t.TempDir()
	must(t, os.WriteFile(filepath.Join(root, "f.txt"), []byte("data"), 0o644))
	cfg := NewConfig()
	cfg.ServerURL = "wss://unused.example.com/ws"
	cfg.Token = "t"
	cfg.AllowedDirs = []string{root}
	cfg.Logger = NopLogger{}
	b, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer b.Shutdown()

	raw, err := b.HandleRequest(context.Background(), []byte(`{"id":1,"method":"read_file","params":{"path":"f.txt"}}`))
	if err != nil {
		t.Fatalf("HandleRequest: %v", err)
	}
	var resp Response
	must(t, json.Unmarshal(raw, &resp))
	if resp.Error != nil {
		t.Fatalf("unexpected error: %+v", resp.Error)
	}
	out, _ := json.Marshal(resp.Result)
	if !strings.Contains(string(out), `"data"`) {
		t.Fatalf("unexpected result: %s", out)
	}
}
