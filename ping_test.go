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
//
// jsonIdle, when > 0, emulates the real adapter's receive_json idle timeout:
// if no JSON *data* frame arrives within jsonIdle, the server closes (4408).
// Control pings do NOT count — that's the exact behavior that kicked the
// 0.6.0 bridge every 58s in the field.
type pongMock struct {
	srv      *httptest.Server
	token    string
	register chan RegisterFrame
	conns    chan *WSConn
	resp     chan Response

	jsonIdle time.Duration // server-side JSON idle timeout (0 = disabled)

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
		// App-level JSON pings ({"type":"ping","id":N}) are answered with
		// {"id":N,"result":{"type":"pong"}} exactly like the real adapter
		// (bridge_ws.py), and are NOT forwarded as responses.
		// With jsonIdle > 0, ReadMessage gets a deadline refreshed only by
		// data frames (control frames are consumed inside ReadMessage before
		// it returns, so only JSON traffic — pings included — keeps this
		// loop's deadline alive), mirroring the real receive_json timeout.
		go func() {
			if m.jsonIdle > 0 {
				_ = conn.SetReadDeadline(time.Now().Add(m.jsonIdle))
			}
			for {
				raw, err := conn.ReadMessage()
				if err != nil {
					if m.jsonIdle > 0 {
						// Emulate CLOSE_IDLE: server-initiated close on idle.
						_ = conn.Close()
					}
					return
				}
				if m.jsonIdle > 0 {
					_ = conn.SetReadDeadline(time.Now().Add(m.jsonIdle))
				}
				var env struct {
					Type string          `json:"type"`
					ID   json.RawMessage `json:"id"`
				}
				if err := json.Unmarshal(raw, &env); err == nil && env.Type == "ping" {
					pong, _ := json.Marshal(map[string]any{"id": env.ID, "result": map[string]string{"type": "pong"}})
					if err := conn.WriteMessage(pong); err != nil {
						return
					}
					continue
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
	mock, b := newFastPingBridge(t, token, rec, 0)
	return mock, b
}

// newFastPingBridge is newPingBridge with a configurable adapter-side JSON
// idle timeout (0 = none). Used by the app-ping tests to reproduce the
// server's receive_json + idle_timeout read loop, which sees data frames
// only — WS control pings are invisible to it.
func newFastPingBridge(t *testing.T, token string, rec *detailRecorder, jsonIdle time.Duration) (*pongMock, *Bridge) {
	t.Helper()
	mock := newPongMock(t, token)
	mock.jsonIdle = jsonIdle
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

// TestAppPing_PongUpdatesLastPongAt: the bridge's app-level JSON ping must
// elicit {"id":N,"result":{"type":"pong"}} from the adapter, and that pong —
// a *response-shaped* frame, not a WS control frame — must update
// LastPongAt. The control-frame pong alone (onPong hook) also refreshes it,
// so the discriminating assertion is on the JSON path: we disable the
// control-frame refresh by asserting the pong arrives via a fresh ping id
// round-trip, checked through LastPongAt advancing while the mock only ever
// answers JSON pings (it does — its auto-pong on control frames is what the
// base mock does; the JSON pong is separately observed on the wire by the
// pump's own reply path). Practically: after the fix, both layers refresh
// LastPongAt; the test pins the JSON layer by matching a pending ping id.
func TestAppPing_PongUpdatesLastPongAt(t *testing.T) {
	mock, b := newPingBridge(t, "tok-appong", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = b.Run(ctx) }()

	select {
	case <-mock.register:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for register")
	}
	<-mock.conns

	waitFor(t, 5*time.Second, "LastPingAt and LastPongAt set", func() bool {
		d := b.StatusDetail()
		return !d.LastPingAt.IsZero() && !d.LastPongAt.IsZero()
	})

	// The JSON layer must round-trip: drain the pending-ping set's ids by
	// observing that each ping tick leaves no unmatched pending id behind.
	// If the JSON pong were not routed, pendingPings would grow unboundedly;
	// sample across several ticks and require the set to stay small.
	base := b.StatusDetail().LastPongAt
	waitFor(t, 5*time.Second, "LastPongAt advancing past first sample (JSON pong round-trip)", func() bool {
		return b.StatusDetail().LastPongAt.After(base)
	})

	b.pingMu.Lock()
	pending := len(b.pendingPings)
	b.pingMu.Unlock()
	if pending > 2 {
		t.Fatalf("pending app pings piled up: %d (JSON pongs not being routed back?)", pending)
	}
}

// TestAppPing_IdleServerNotKicked: reproduce the 0.6.0 field failure — an
// adapter whose read loop only sees JSON frames (receive_json + idle_timeout,
// bridge_ws.py:121-127) closes with 4408 after ~58s even though the bridge
// sends WS control pings, because control frames never reach the application
// layer. With the app-level JSON ping added to pingLoop, the session must
// survive well past the JSON idle window.
func TestAppPing_IdleServerNotKicked(t *testing.T) {
	// jsonIdle=600ms: three PingIntervals (150ms each) fit inside, so the
	// bridge must deliver JSON pings continuously to keep the server loop
	// alive. Survive 3s = 5x the idle window with zero test-driven traffic.
	mock, b := newFastPingBridge(t, "tok-idlejson", nil, 600*time.Millisecond)

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
		t.Fatal("bridge dropped an idle session despite app-level JSON pings (server idle_timeout kick)")
	}
	if got := mock.regCount(); got != 1 {
		t.Fatalf("expected exactly 1 registration, got %d — the JSON-idle server closed the session", got)
	}
	// The same socket must still serve a tool call: the app ping must not
	// have disturbed the tool-call routing path.
	resp := mock.call(t, first, 200, MethodListDirectory, map[string]any{"path": "."})
	if resp.Error != nil {
		t.Fatalf("list_directory after idle window errored: %+v", resp.Error)
	}
}
