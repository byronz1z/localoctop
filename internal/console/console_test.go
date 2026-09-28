package console

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	octobridge "github.com/byronz1z/octop-local-bridge"
	"github.com/byronz1z/octop-local-bridge/internal/appcfg"
)

// startTestServer runs a console Server on a random loopback port.
func startTestServer(t *testing.T, cfg appcfg.File, apply func(appcfg.File) error) (*Server, string) {
	t.Helper()
	if apply == nil {
		apply = func(appcfg.File) error { return nil }
	}
	srv := New(cfg, apply)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go srv.Serve(ln)
	t.Cleanup(func() {
		srv.Shutdown()
		ln.Close()
	})
	return srv, "http://" + ln.Addr().String()
}

func TestListenProbesOnConflict(t *testing.T) {
	occupied, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("occupy port: %v", err)
	}
	defer occupied.Close()
	port := occupied.Addr().(*net.TCPAddr).Port

	got, err := Listen(port, 3)
	if err != nil {
		t.Fatalf("Listen(%d, 3): %v", port, err)
	}
	defer got.Close()
	if got.Addr().(*net.TCPAddr).Port == port {
		t.Errorf("Listen returned the occupied port %d", port)
	}
}

func TestListenBindsLoopbackOnly(t *testing.T) {
	ln, err := Listen(0, 3)
	if err != nil {
		t.Fatalf("Listen: %v", err)
	}
	defer ln.Close()
	host, _, err := net.SplitHostPort(ln.Addr().String())
	if err != nil {
		t.Fatalf("split addr: %v", err)
	}
	if host != "127.0.0.1" {
		t.Errorf("console bound to %q, want 127.0.0.1 only", host)
	}
}

func TestConfigGetUnconfigured(t *testing.T) {
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Get(base + "/api/config")
	if err != nil {
		t.Fatalf("GET config: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var view configView
	if err := json.NewDecoder(resp.Body).Decode(&view); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if view.Configured {
		t.Error("empty settings must report configured=false (onboarding)")
	}
	if view.ConsolePort != appcfg.DefaultConsolePort {
		t.Errorf("console_port = %d, want default %d", view.ConsolePort, appcfg.DefaultConsolePort)
	}
}

func TestConfigPostAppliesAndReflects(t *testing.T) {
	var applied appcfg.File
	calls := 0
	srv, base := startTestServer(t, appcfg.Default(), func(c appcfg.File) error {
		applied = c
		calls++
		return nil
	})

	body := `{"server_url":"wss://example.com/mcp-localfs/ws","token":"t0",
	  "allowed_dirs":[{"path":"C:/tmp/x","enabled":true},{"path":"C:/tmp/off","enabled":false}],
	  "allow_write":false,"console_port":19881,"open_browser":false}`
	resp, err := http.Post(base+"/api/config", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST config: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST status = %d, want 200", resp.StatusCode)
	}
	if calls != 1 {
		t.Fatalf("apply called %d times, want 1", calls)
	}
	if applied.ServerURL != "wss://example.com/mcp-localfs/ws" || applied.Token != "t0" {
		t.Errorf("apply got url=%q token=%q", applied.ServerURL, applied.Token)
	}
	// AuditLogPath must survive a save the UI does not know about.
	srv.SetConfig(applied)

	resp, err = http.Get(base + "/api/config")
	if err != nil {
		t.Fatalf("GET config: %v", err)
	}
	defer resp.Body.Close()
	var view configView
	if err := json.NewDecoder(resp.Body).Decode(&view); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !view.Configured {
		t.Error("after save with url+token+enabled dir, configured = false")
	}
	if len(view.AllowedDirs) != 2 || !view.AllowedDirs[0].Enabled || view.AllowedDirs[1].Enabled {
		t.Errorf("allowed_dirs round-trip mismatch: %+v", view.AllowedDirs)
	}
	if view.ConsolePort != 19881 {
		t.Errorf("console_port = %d, want 19881", view.ConsolePort)
	}
}

func TestConfigPostApplyErrorSurfaces(t *testing.T) {
	_, base := startTestServer(t, appcfg.Default(), func(appcfg.File) error {
		return fmt.Errorf("config: Token is required")
	})
	resp, err := http.Post(base+"/api/config", "application/json", strings.NewReader(`{"server_url":"wss://x/ws"}`))
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	var out map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !strings.Contains(out["error"], "Token is required") {
		t.Errorf("error = %q, want the apply error surfaced", out["error"])
	}
}

func TestAboutEndpoint(t *testing.T) {
	srv, base := startTestServer(t, appcfg.Default(), nil)
	srv.SetAbout(About{
		Version:    "0.1.0",
		ConsoleURL: "http://127.0.0.1:19880",
		ConfigPath: "/tmp/config.json",
		AuditPath:  "/tmp/audit.jsonl",
	})
	resp, err := http.Get(base + "/api/about")
	if err != nil {
		t.Fatalf("GET about: %v", err)
	}
	defer resp.Body.Close()
	var a About
	if err := json.NewDecoder(resp.Body).Decode(&a); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if a.Version != "0.1.0" || a.ConsoleURL != "http://127.0.0.1:19880" {
		t.Errorf("about = %+v", a)
	}
}

func TestStatusSnapshotNoLockCopy(t *testing.T) {
	// Regression for the vet copylocks finding: snapshots are plain values.
	var st status
	st.update(func(x *Status) { x.Connected = true; x.Reconnects = 3 })
	got := st.snapshot()
	if !got.Connected || got.Reconnects != 3 {
		t.Errorf("snapshot = %+v", got)
	}
}

// sseFrame is one parsed Server-Sent Event.
type sseFrame struct{ name, data string }

// readSSEFrames opens /api/events over a raw socket (so the read deadline
// applies) and collects frames until want frames arrive or the deadline hits.
func readSSEFrames(base string, want int) ([]sseFrame, error) {
	addr := strings.TrimPrefix(base, "http://")
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	fmt.Fprintf(conn, "GET /api/events HTTP/1.1\r\nHost: %s\r\nAccept: text/event-stream\r\n\r\n", addr)

	br := bufio.NewReader(conn)
	// Skip response headers.
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return nil, fmt.Errorf("read headers: %w", err)
		}
		if strings.TrimRight(line, "\r\n") == "" {
			break
		}
	}
	var frames []sseFrame
	var cur sseFrame
	for len(frames) < want {
		line, err := br.ReadString('\n')
		if err != nil {
			return nil, fmt.Errorf("read frame %d: %w", len(frames), err)
		}
		line = strings.TrimRight(line, "\r\n")
		switch {
		case line == "":
			if cur.name != "" || cur.data != "" {
				frames = append(frames, cur)
				cur = sseFrame{}
			}
		case strings.HasPrefix(line, "event: "):
			cur.name = strings.TrimPrefix(line, "event: ")
		case strings.HasPrefix(line, "data: "):
			cur.data = strings.TrimPrefix(line, "data: ")
		}
	}
	return frames, nil
}

func TestSSEInitialStatusThenPushes(t *testing.T) {
	srv, base := startTestServer(t, appcfg.Default(), nil)

	type result struct {
		frames []sseFrame
		err    error
	}
	ch := make(chan result, 1)
	go func() {
		// 3 frames: initial status, pushed status, pushed audit.
		frames, err := readSSEFrames(base, 3)
		ch <- result{frames: frames, err: err}
	}()

	// Give the subscriber a moment to attach before pushing.
	time.Sleep(200 * time.Millisecond)
	srv.PushStatus(true, "")
	srv.PushAudit(octobridge.AuditEvent{
		Time:     "2026-09-29T00:00:00Z",
		Method:   "read_file",
		Path:     "C:/tmp/x/a.txt",
		Decision: "allow",
	})

	var res result
	select {
	case res = <-ch:
	case <-time.After(6 * time.Second):
		t.Fatal("timed out waiting for SSE frames")
	}
	if res.err != nil {
		t.Fatal(res.err)
	}
	f := res.frames
	if f[0].name != "status" {
		t.Errorf("frame 0 = %q, want initial status", f[0].name)
	}
	var st Status
	if err := json.Unmarshal([]byte(f[1].data), &st); err != nil {
		t.Fatalf("frame 1 data: %v", err)
	}
	if f[1].name != "status" || !st.Connected {
		t.Errorf("frame 1 = %q %+v, want connected status", f[1].name, st)
	}
	var ev octobridge.AuditEvent
	if err := json.Unmarshal([]byte(f[2].data), &ev); err != nil {
		t.Fatalf("frame 2 data: %v", err)
	}
	if f[2].name != "audit" || ev.Method != "read_file" || ev.Decision != "allow" {
		t.Errorf("frame 2 = %q %+v, want the pushed audit event", f[2].name, ev)
	}
}

func TestIndexServesHTMLAndRejectsOtherPaths(t *testing.T) {
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Get(base + "/")
	if err != nil {
		t.Fatalf("GET /: %v", err)
	}
	body := make([]byte, 512)
	n, _ := resp.Body.Read(body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || !strings.Contains(string(body[:n]), "Octop Local Bridge") {
		t.Errorf("GET / status=%d, body does not look like the console page", resp.StatusCode)
	}
	if got := resp.Header.Get("X-Frame-Options"); got != "DENY" {
		t.Errorf("X-Frame-Options = %q, want DENY", got)
	}

	resp, err = http.Get(base + "/nope")
	if err != nil {
		t.Fatalf("GET /nope: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("GET /nope status = %d, want 404", resp.StatusCode)
	}
}
