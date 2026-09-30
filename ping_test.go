package localoctop

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// waitFor polls cond until it holds or the timeout fires; it Fatalfs on
// timeout so callers stay one-liners.
func waitFor(t *testing.T, timeout time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s (timeout %s)", what, timeout)
}

// detailRecorder collects every StatusDetail handed to OnStatusDetail.
type detailRecorder struct {
	mu      sync.Mutex
	history []StatusDetail
}

func (r *detailRecorder) record(d StatusDetail) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.history = append(r.history, d)
}

func (r *detailRecorder) snapshot() []StatusDetail {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]StatusDetail(nil), r.history...)
}

// pongMock is a mock adapter whose server side keeps reading after register,
// so inbound pings get auto-ponged (WSConn.ReadMessage's control-frame
// handling). The stock mockAdapter hands the socket to the test and never
// reads again — fine for request/response tests, useless for keep-alive.
// Data frames (bridge responses) are forwarded on resp; the test writes
// requests directly on the conn.
type pongMock struct {
	srv      *httptest.Server
	token    string
	register chan RegisterFrame
	conns    chan *WSConn
	resp     chan Response

	regCountAtomic int64 // total registrations seen (survives channel drains)
}

func newPongMock(t *testing.T, token string) *pongMock {
	t.Helper()
	m := &pongMock{
		token:    token,
		register: make(chan RegisterFrame, 8),
		conns:    make(chan *WSConn, 8),
		resp:     make(chan Response, 8),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/mcp/localoctop/ws", func(w http.ResponseWriter, r *http.Request) {
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
		atomic.AddInt64(&m.regCountAtomic, 1)
		select {
		case m.register <- reg:
		case <-time.After(time.Second):
		}
		select {
		case m.conns <- conn:
		default:
		}
		// Pump: auto-pongs pings, forwards data frames as responses.
		go func() {
			for {
				raw, err := conn.ReadMessage()
				if err != nil {
					return
				}
				var resp Response
				if err := json.Unmarshal(raw, &resp); err != nil || len(resp.ID) == 0 {
					continue
				}
				select {
				case m.resp <- resp:
				default:
				}
			}
		}()
	})
	m.srv = httptest.NewServer(mux)
	t.Cleanup(func() { m.srv.Close() })
	return m
}

func (m *pongMock) wsURL() string {
	return "ws" + strings.TrimPrefix(m.srv.URL, "http") + "/mcp/localoctop/ws"
}

// regCount reports how many registrations the mock has seen in total.
func (m *pongMock) regCount() int {
	return int(atomic.LoadInt64(&m.regCountAtomic))
}

// call pushes a tool call and waits for the matching response (the pump
// forwards all of them; IDs disambiguate).
func (m *pongMock) call(t *testing.T, conn *WSConn, id int, method string, params map[string]any) Response {
	t.Helper()
	frame, _ := json.Marshal(map[string]any{"id": id, "method": method, "params": params})
	if err := conn.WriteMessage(frame); err != nil {
		t.Fatalf("mock write: %v", err)
	}
	deadline := time.After(10 * time.Second)
	for {
		select {
		case resp := <-m.resp:
			var i int
			if json.Unmarshal(resp.ID, &i) == nil && i == id {
				return resp
			}
		case <-deadline:
			t.Fatalf("timed out waiting for response to id %d", id)
		}
	}
}

// newPingBridge wires a bridge against a pongMock with fast keep-alive knobs
// (PingInterval=150ms, PongWait=500ms) and an optional detail recorder.
func newPingBridge(t *testing.T, token string, rec *detailRecorder) (*pongMock, *Bridge) {
	t.Helper()
	mock := newPongMock(t, token)
	root := t.TempDir()

	cfg := NewConfig()
	cfg.ServerURL = mock.wsURL()
	cfg.Token = token
	cfg.AllowedDirs = []string{root}
	cfg.Logger = NopLogger{}
	cfg.MinBackoff = 50 * time.Millisecond
	cfg.MaxBackoff = 200 * time.Millisecond
	cfg.PingInterval = 150 * time.Millisecond
	cfg.PongWait = 500 * time.Millisecond
	if rec != nil {
		cfg.OnStatusDetail = rec.record
	}
	b, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(b.Shutdown)
	return mock, b
}

// TestPingLoop_KeepsIdleAlive: with PongWait=500ms and zero traffic from the
// test, only the bridge's own pings can keep the session up. We idle for 3s
// (6x PongWait, well past 2x PingInterval) and then prove the connection
// never dropped: still Connected, no second registration, and the original
// socket still serves a tool call. Without pingLoop the readLoop deadline
// would trip at ~500ms and the mock would see a re-register.
func TestPingLoop_KeepsIdleAlive(t *testing.T) {
	mock, b := newPingBridge(t, "tok-idle", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	first := <-mock.conns

	time.Sleep(3 * time.Second)

	if !b.Connected() {
		t.Fatal("bridge dropped an idle session despite pingLoop keep-alive")
	}
	if got := mock.regCount(); got != 1 {
		t.Fatalf("expected exactly 1 registration, got %d — idle session was dropped and redialed", got)
	}
	resp := mock.call(t, first, 100, MethodListDirectory, map[string]any{"path": "."})
	if resp.Error != nil {
		t.Fatalf("list_directory after idle window errored: %+v", resp.Error)
	}
}

// TestPingLoop_WriteFailureTriggersReconnect: the session dies behind the
// bridge's back (server side closed — pending writes fail, blocked reads
// error out). pingLoop / readLoop must tear the connection down and the Run
// loop must redial and re-register, and the new session must serve calls.
func TestPingLoop_WriteFailureTriggersReconnect(t *testing.T) {
	mock, b := newPingBridge(t, "tok-pingfail", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	first := <-mock.conns

	// Simulate a dead socket: close it so the client's next Ping (≤150ms)
	// or read deadline fails.
	first.Close()

	waitFor(t, 5*time.Second, "re-registration after socket death", func() bool {
		return mock.regCount() >= 2
	})
	var second *WSConn
	select {
	case second = <-mock.conns:
	default:
		t.Fatal("no fresh connection after re-registration")
	}
	if second == first {
		t.Fatal("expected a fresh connection object after ping failure")
	}
	resp := mock.call(t, second, 101, MethodListDirectory, map[string]any{"path": "."})
	if resp.Error != nil {
		t.Fatalf("list_directory after reconnect errored: %+v", resp.Error)
	}
}

// TestStatusDetail_ReconnectSequence: dial failure climbs ReconnectAttempt
// and fills LastError; a successful connect resets both; a drop climbs
// again. Full event history asserted over one shared recorder.
func TestStatusDetail_ReconnectSequence(t *testing.T) {
	rec := &detailRecorder{}

	// Stage 1: dead endpoint — an httptest server closed immediately, so
	// dials fail fast with connection-refused.
	deadSrv := httptest.NewServer(http.NotFoundHandler())
	deadURL := "ws" + strings.TrimPrefix(deadSrv.URL, "http") + "/mcp/localoctop/ws"
	deadSrv.Close()

	root := t.TempDir()
	cfg := NewConfig()
	cfg.ServerURL = deadURL
	cfg.Token = "tok-seq"
	cfg.AllowedDirs = []string{root}
	cfg.Logger = NopLogger{}
	cfg.MinBackoff = 50 * time.Millisecond
	cfg.MaxBackoff = 200 * time.Millisecond
	cfg.PingInterval = 100 * time.Millisecond
	cfg.PongWait = 1 * time.Second
	cfg.OnStatusDetail = rec.record
	b, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(b.Shutdown)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	waitFor(t, 5*time.Second, "ReconnectAttempt >= 2 against dead endpoint", func() bool {
		return b.StatusDetail().ReconnectAttempt >= 2
	})
	d := b.StatusDetail()
	if d.Connected {
		t.Fatal("must not report Connected against a dead endpoint")
	}
	if d.LastError == "" {
		t.Fatal("LastError must be populated while failing to dial")
	}

	// Stage 2: a live bridge (same recorder) goes through the reset
	// transition. ServerURL is fixed at New time, so "the endpoint comes
	// back" is modeled by a second bridge against a live mock.
	mock, b2 := newPingBridge(t, "tok-seq2", rec)
	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()
	go func() { _ = b2.Run(ctx2) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register on live mock")
	}
	waitFor(t, 5*time.Second, "Connected with attempt reset to 0", func() bool {
		d := b2.StatusDetail()
		return d.Connected && d.ReconnectAttempt == 0 && d.LastError == ""
	})

	// Stage 3: drop the session; attempts climb again.
	conn := <-mock.conns
	conn.Close()
	waitFor(t, 5*time.Second, "ReconnectAttempt >= 1 after drop", func() bool {
		return b2.StatusDetail().ReconnectAttempt >= 1
	})

	// Event-sequence sanity over the shared recorder: at least one failed
	// attempt before the first successful connect, and one after it.
	var sawDead, sawLive, sawDrop bool
	for _, e := range rec.snapshot() {
		if !e.Connected && e.ReconnectAttempt >= 1 {
			if !sawLive {
				sawDead = true
			} else {
				sawDrop = true
			}
		}
		if e.Connected && e.ReconnectAttempt == 0 {
			sawLive = true
		}
	}
	if !sawDead || !sawLive || !sawDrop {
		t.Fatalf("event sequence incomplete: dead=%v live=%v drop=%v (history has %d events)",
			sawDead, sawLive, sawDrop, len(rec.snapshot()))
	}
}

// TestStatusDetail_PingPongTimestamps: once connected, LastPingAt and
// LastPongAt must both be set (the mock's pump auto-pongs), keep advancing,
// and the pong must not predate its ping.
func TestStatusDetail_PingPongTimestamps(t *testing.T) {
	mock, b := newPingBridge(t, "tok-ts", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	<-mock.conns

	start := time.Now()
	waitFor(t, 5*time.Second, "LastPingAt and LastPongAt set", func() bool {
		d := b.StatusDetail()
		return !d.LastPingAt.IsZero() && !d.LastPongAt.IsZero()
	})
	d := b.StatusDetail()
	if d.LastPingAt.Before(start) || d.LastPongAt.Before(start) {
		t.Fatalf("timestamps predate the session: ping=%v pong=%v start=%v", d.LastPingAt, d.LastPongAt, start)
	}
	if d.LastPongAt.Before(d.LastPingAt) {
		t.Fatalf("pong (%v) predates ping (%v)", d.LastPongAt, d.LastPingAt)
	}

	firstPing := d.LastPingAt
	waitFor(t, 5*time.Second, "LastPingAt advancing past first sample", func() bool {
		return b.StatusDetail().LastPingAt.After(firstPing)
	})
}
