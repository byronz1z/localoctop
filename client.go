package localoctop

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"
)

// Bridge is the outbound WSS client. It dials the localoctop adapter,
// registers under its token, and serves tool calls until the context is
// cancelled. On any disconnect it reconnects with exponential backoff
// (1s → 2s → … → 60s cap) and re-registers automatically.
//
// Typical embedding (this is exactly what cmd/bridge/main.go does):
//
//	bridge := localoctop.New(localoctop.Config{
//	    ServerURL:   "wss://octop.example.com/mcp/localoctop/ws",
//	    Token:       userToken,
//	    AllowedDirs: []string{"~/Documents/Octop"},
//	})
//	go bridge.Run(ctx)
type Bridge struct {
	cfg   Config
	log   Logger
	guard *pathGuard
	tools *tools
	audit *AuditLogger

	clientID string

	mu     sync.Mutex
	conn   *WSConn
	closed bool

	// statusMu guards connection-state fields shared with Connected()/OnStatus.
	statusMu    sync.Mutex
	connected   bool
	lastHealthy bool

	// shutdownCh is closed by Shutdown so Run's backoff sleep can exit early.
	shutdownCh chan struct{}
}

// New validates cfg and builds a Bridge. It never dials; call Run to start.
func New(cfg Config) (*Bridge, error) {
	if cfg.Logger == nil {
		cfg.Logger = NewStderrLogger(false)
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	clientID, err := newClientID()
	if err != nil {
		return nil, fmt.Errorf("localoctop: generate client id: %w", err)
	}
	guard := newPathGuard(cfg.AllowedDirs, cfg.Logger)
	audit, err := NewAuditLogger(cfg.AuditLogPath, cfg.AuditMaxBytes, cfg.Logger, cfg.OnAudit)
	if err != nil {
		return nil, err
	}
	return &Bridge{
		cfg:      cfg,
		log:      cfg.Logger,
		guard:    guard,
		tools:    newTools(cfg, guard, cfg.Logger),
		audit:    audit,
		clientID: clientID,

		shutdownCh: make(chan struct{}),
	}, nil
}

// isClosed reports whether Shutdown has been called.
func (b *Bridge) isClosed() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.closed
}

// ClientID returns this bridge instance's random identifier, stable for the
// process lifetime and reported in register frames.
func (b *Bridge) ClientID() string { return b.clientID }

// Connected reports whether the WebSocket session is currently up.
func (b *Bridge) Connected() bool {
	b.statusMu.Lock()
	defer b.statusMu.Unlock()
	return b.connected
}

func (b *Bridge) setConnected(v bool, err error) {
	b.statusMu.Lock()
	changed := b.connected != v
	b.connected = v
	b.statusMu.Unlock()
	if changed && b.cfg.OnStatus != nil {
		b.cfg.OnStatus(v, err)
	}
}

// Run blocks until ctx is cancelled, maintaining the connection with
// exponential backoff. It is safe to call from a goroutine exactly once.
func (b *Bridge) Run(ctx context.Context) error {
	defer b.Shutdown()
	backoff := b.cfg.MinBackoff
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if b.isClosed() {
			return nil
		}
		err := b.runOnce(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		b.setConnected(false, err)
		if err != nil {
			b.log.Warnf("bridge session ended: %v (reconnect in %s)", err, backoff)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-b.shutdownCh:
			return nil
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > b.cfg.MaxBackoff {
			backoff = b.cfg.MaxBackoff
		}
		// (backoff resets to min after a successful session — see runOnce.)
		if b.lastSessionSucceeded() {
			backoff = b.cfg.MinBackoff
		}
	}
}

// lastSessionSucceeded reports whether the most recent session stayed up
// long enough to count as "healthy", so we don't hold a 60s penalty after a
// transient blip that lasted minutes.
func (b *Bridge) lastSessionSucceeded() bool {
	b.statusMu.Lock()
	defer b.statusMu.Unlock()
	return b.lastHealthy
}

// runOnce dials, registers, and serves until the connection dies or ctx ends.
func (b *Bridge) runOnce(ctx context.Context) error {
	// Reset health at the *start* of each attempt so consecutive dial
	// failures correctly escalate the backoff ladder.
	b.statusMu.Lock()
	b.lastHealthy = false
	b.statusMu.Unlock()

	dialCtx, cancel := context.WithTimeout(ctx, b.cfg.DialTimeout+5*time.Second)
	defer cancel()

	start := time.Now()
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+b.cfg.Token)
	headers.Set("User-Agent", "localoctop/"+Version)
	conn, err := dialWS(dialCtx, b.cfg.ServerURL, headers, b.cfg.DialTimeout, nil, b.cfg.MaxMsgBytes)
	if err != nil {
		return err
	}
	b.mu.Lock()
	b.conn = conn
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		b.conn = nil
		b.mu.Unlock()
		conn.Close()
	}()

	// Register: binds this socket to the token's user on the adapter side.
	if err := b.sendRegister(conn); err != nil {
		return fmt.Errorf("register failed: %w", err)
	}
	b.setConnected(true, nil)
	b.log.Infof("bridge connected to %s (client_id=%s)", b.cfg.ServerURL, b.clientID)

	// Keep-alive pings.
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		t := time.NewTicker(b.cfg.PingInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-conn.Closed():
				return
			case <-t.C:
				if err := conn.Ping(); err != nil {
					b.log.Debugf("ping failed: %v", err)
					return
				}
			}
		}
	}()

	err = b.readLoop(ctx, conn)
	// Close immediately so the ping goroutine unblocks at once; otherwise a
	// dead socket could delay reconnection by up to PingInterval.
	conn.Close()
	if time.Since(start) > b.cfg.MaxBackoff {
		// Session lived longer than the backoff cap: treat as healthy so the
		// next reconnect starts at MinBackoff again.
		b.statusMu.Lock()
		b.lastHealthy = true
		b.statusMu.Unlock()
	}
	<-pingDone
	return err
}

func (b *Bridge) sendRegister(conn *WSConn) error {
	host, _ := os.Hostname()
	frame := RegisterFrame{
		Type:       "register",
		Token:      b.cfg.Token,
		ClientID:   b.clientID,
		Hostname:   host,
		AllowedDir: b.cfg.AllowedDirs,
		WriteMode:  b.cfg.AllowWrite,
		Version:    Version,
	}
	data, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	return conn.WriteMessage(data)
}

// readLoop serves requests until the socket breaks or ctx is cancelled.
func (b *Bridge) readLoop(ctx context.Context, conn *WSConn) error {
	// The idle timeout refreshes on every inbound frame (including pongs),
	// so a healthy but chatty-keepalive-only session is not killed, while a
	// truly dead socket trips the deadline within PongWait.
	conn.SetIdleTimeout(b.cfg.PongWait)
	_ = conn.SetReadDeadline(time.Now().Add(b.cfg.PongWait))

	// Watch for ctx cancellation: ReadMessage blocks on the socket, so a
	// separate goroutine closes it when Run's context ends.
	ctxDone := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			conn.Close()
		case <-ctxDone:
		}
	}()
	defer close(ctxDone)

	for {
		raw, err := conn.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}
		go b.handleFrame(conn, raw)
	}
}

// handleFrame decodes and dispatches one inbound frame. Tool calls run in
// their own goroutine (bounded by RequestWait) so a slow disk read cannot
// stall the socket's read deadline.
func (b *Bridge) handleFrame(conn *WSConn, raw []byte) {
	var req Request
	if err := json.Unmarshal(raw, &req); err != nil {
		b.log.Warnf("dropping malformed frame: %v", err)
		return
	}
	switch req.FrameType {
	case "ping":
		b.respond(conn, Response{ID: req.ID, Result: map[string]string{"type": "pong", "client_id": b.clientID}})
		return
	case "shutdown":
		b.log.Infof("adapter requested shutdown")
		conn.Close()
		return
	}

	start := time.Now()
	callCtx, cancel := context.WithTimeout(context.Background(), b.cfg.RequestWait)
	defer cancel()

	result, berr := b.dispatch(callCtx, req.Method, req.Params)
	resp := Response{ID: req.ID, Result: result}
	decision := "allow"
	code := CodeOK
	detail := ""
	if berr != nil {
		resp.Result = nil
		resp.Error = berr.ToWire()
		code = berr.Code
		detail = berr.Error()
		if code == CodeInternal || code == CodeTimeout {
			decision = "error"
		} else {
			decision = "deny"
		}
		b.log.Warnf("%s denied: %s", req.Method, detail)
	}

	b.audit.Record(AuditEvent{
		Method:   req.Method,
		Path:     pathOf(req.Params),
		Decision: decision,
		Code:     code,
		Detail:   detail,
		Duration: time.Since(start).String(),
		ClientID: b.clientID,
	})

	if err := b.respond(conn, resp); err != nil {
		b.log.Errorf("send response for %s: %v", req.Method, err)
	}
}

// dispatch routes a method name to its handler.
func (b *Bridge) dispatch(ctx context.Context, method string, params map[string]any) (any, *BridgeError) {
	// Cooperative cancellation check for long walks.
	if err := ctx.Err(); err != nil {
		return nil, &BridgeError{Code: CodeTimeout, Message: "request context expired before start"}
	}
	switch method {
	case MethodListDirectory:
		return b.tools.listDirectory(params)
	case MethodReadFile:
		return b.tools.readFile(params)
	case MethodSearchFiles:
		return b.tools.searchFiles(params)
	case MethodGetFileInfo:
		return b.tools.getFileInfo(params)
	case MethodWriteFile:
		return b.tools.writeFile(params)
	case MethodCreateDir:
		return b.tools.createDirectory(params)
	default:
		return nil, errMethodNotFound(method)
	}
}

func (b *Bridge) respond(conn *WSConn, resp Response) error {
	data, err := json.Marshal(resp)
	if err != nil {
		return fmt.Errorf("marshal response: %w", err)
	}
	return conn.WriteMessage(data)
}

// Shutdown closes the active connection and the audit log. Safe to call more
// than once and from any goroutine.
func (b *Bridge) Shutdown() {
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		return
	}
	b.closed = true
	conn := b.conn
	b.conn = nil
	close(b.shutdownCh)
	b.mu.Unlock()
	if conn != nil {
		conn.Close()
	}
	b.setConnected(false, nil)
	if err := b.audit.Close(); err != nil {
		b.log.Warnf("audit close: %v", err)
	}
}

// HandleRequest is exported for tests and for embedders that want to drive the
// bridge over a transport other than WebSocket (e.g. in-process IPC). It
// executes one request and returns the serializable response.
func (b *Bridge) HandleRequest(ctx context.Context, raw []byte) ([]byte, error) {
	var req Request
	if err := json.Unmarshal(raw, &req); err != nil {
		return nil, fmt.Errorf("localoctop: malformed request: %w", err)
	}
	start := time.Now()
	result, berr := b.dispatch(ctx, req.Method, req.Params)
	resp := Response{ID: req.ID}
	decision, code, detail := "allow", CodeOK, ""
	if berr != nil {
		resp.Error = berr.ToWire()
		code, detail = berr.Code, berr.Error()
		if code == CodeInternal || code == CodeTimeout {
			decision = "error"
		} else {
			decision = "deny"
		}
	} else {
		resp.Result = result
	}
	b.audit.Record(AuditEvent{
		Method:   req.Method,
		Path:     pathOf(req.Params),
		Decision: decision,
		Code:     code,
		Detail:   detail,
		Duration: time.Since(start).String(),
		ClientID: b.clientID,
	})
	return json.Marshal(resp)
}

func pathOf(params map[string]any) string {
	if params == nil {
		return ""
	}
	if p, ok := params["path"].(string); ok {
		return p
	}
	return ""
}

// newClientID returns 8 random hex bytes, e.g. "3f9a2c1d4b5e6f70".
func newClientID() (string, error) {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}
