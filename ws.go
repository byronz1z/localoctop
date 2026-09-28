package octobridge

// Minimal, dependency-free RFC 6455 WebSocket implementation. Only the
// subset the bridge needs is covered: text/binary data messages, ping/pong
// keep-alive, close handshake, fragmented reads, and masked client writes.
// Keeping this in-module means `go build ./...` works fully offline — no
// module downloads, no vendor tree, nothing outside the Go standard library.

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const websocketGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

// Frame opcodes (RFC 6455 §5.2).
const (
	opContinuation byte = 0x0
	opText         byte = 0x1
	opBinary       byte = 0x2
	opClose        byte = 0x8
	opPing         byte = 0x9
	opPong         byte = 0xA
)

// Close status codes used by this package.
const (
	closeNormal   uint16 = 1000
	closeGoingAwy uint16 = 1001
	closeProtocol uint16 = 1002
	closePolicy   uint16 = 1008
	closeTooBig   uint16 = 1009
)

// ErrConnClosed is returned by write operations after the connection closed.
var ErrConnClosed = errors.New("octobridge: websocket connection closed")

// CloseError reports a close frame received from the peer.
type CloseError struct {
	Code   uint16
	Reason string
}

func (e *CloseError) Error() string {
	return fmt.Sprintf("octobridge: peer closed websocket (code=%d reason=%q)", e.Code, e.Reason)
}

// IsCloseError reports whether err is a peer close frame.
func IsCloseError(err error) bool {
	var ce *CloseError
	return errors.As(err, &ce)
}

// WSConn is a framed WebSocket connection. A single reader goroutine and a
// single writer goroutine may operate concurrently; writes are serialized by
// an internal mutex.
type WSConn struct {
	conn     net.Conn
	br       *bufio.Reader
	isClient bool
	maxMsg   int64

	// idleTimeout, when > 0, refreshes the connection read deadline after
	// every successfully read frame (including control frames). Without it,
	// a connection that only exchanges ping/pong would hit the deadline set
	// before ReadMessage even though traffic is flowing. Set once before the
	// read loop starts; not mutated afterwards.
	idleTimeout time.Duration

	wmu       sync.Mutex // serializes frame writes
	closeOnce sync.Once
	closed    chan struct{}

	onPong func() // optional hook invoked when a pong frame arrives
}

// SetIdleTimeout makes every successfully read frame push the read deadline
// to now+d, so keep-alive traffic alone keeps the session alive. Pass 0 to
// disable (deadlines then behave as plain SetReadDeadline calls).
func (c *WSConn) SetIdleTimeout(d time.Duration) { c.idleTimeout = d }

func newWSConn(conn net.Conn, br *bufio.Reader, isClient bool, maxMsg int64) *WSConn {
	if maxMsg <= 0 {
		maxMsg = DefaultMaxMsgBytes
	}
	return &WSConn{
		conn:     conn,
		br:       br,
		isClient: isClient,
		maxMsg:   maxMsg,
		closed:   make(chan struct{}),
	}
}

// SetReadDeadline / SetWriteDeadline pass through to the underlying conn.
func (c *WSConn) SetReadDeadline(t time.Time) error  { return c.conn.SetReadDeadline(t) }
func (c *WSConn) SetWriteDeadline(t time.Time) error { return c.conn.SetWriteDeadline(t) }

// Closed returns a channel closed when this connection is torn down.
func (c *WSConn) Closed() <-chan struct{} { return c.closed }

// ---- reading ----

func (c *WSConn) readFrame() (fin bool, opcode byte, payload []byte, err error) {
	var hdr [2]byte
	if _, err = io.ReadFull(c.br, hdr[:]); err != nil {
		return false, 0, nil, err
	}
	if rsv := hdr[0] & 0x70; rsv != 0 {
		return false, 0, nil, c.protocolError(fmt.Sprintf("non-zero RSV bits %#x", rsv))
	}
	opcode = hdr[0] & 0x0f
	fin = hdr[0]&0x80 != 0
	masked := hdr[1]&0x80 != 0

	length := int64(hdr[1] & 0x7f)
	switch length {
	case 126:
		var ext [2]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return false, 0, nil, err
		}
		length = int64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return false, 0, nil, err
		}
		v := binary.BigEndian.Uint64(ext[:])
		if v > uint64(math.MaxInt64) {
			return false, 0, nil, c.protocolError("frame length overflows int64")
		}
		length = int64(v)
	}

	control := opcode >= 0x8
	if control {
		if !fin {
			return false, 0, nil, c.protocolError("fragmented control frame")
		}
		if length > 125 {
			return false, 0, nil, c.protocolError("control frame exceeds 125 bytes")
		}
	} else if opcode != opText && opcode != opBinary && opcode != opContinuation {
		return false, 0, nil, c.protocolError(fmt.Sprintf("unknown opcode %#x", opcode))
	} else if length > c.maxMsg {
		return false, 0, nil, c.tooBigError(length)
	}

	// Clients MUST receive unmasked frames; servers MUST receive masked ones.
	if masked == c.isClient {
		return false, 0, nil, c.protocolError("incorrect frame masking for this role")
	}

	var maskKey [4]byte
	if masked {
		if _, err = io.ReadFull(c.br, maskKey[:]); err != nil {
			return false, 0, nil, err
		}
	}
	payload = make([]byte, length)
	if _, err = io.ReadFull(c.br, payload); err != nil {
		return false, 0, nil, err
	}
	if masked {
		applyMask(maskKey, payload)
	}
	// Any successfully-read frame (data or control) counts as liveness.
	if c.idleTimeout > 0 {
		_ = c.conn.SetReadDeadline(time.Now().Add(c.idleTimeout))
	}
	return fin, opcode, payload, nil
}

// ReadMessage reads the next complete data message, transparently handling
// fragmentation and control frames (auto-pong on ping, error on close).
func (c *WSConn) ReadMessage() ([]byte, error) {
	var (
		msg []byte
	)
	for {
		fin, opcode, payload, err := c.readFrame()
		if err != nil {
			return nil, err
		}
		switch opcode {
		case opPing:
			if werr := c.writeFrame(true, opPong, payload); werr != nil {
				return nil, werr
			}
			continue
		case opPong:
			if c.onPong != nil {
				c.onPong()
			}
			continue
		case opClose:
			code := closeNormal
			reason := ""
			if len(payload) >= 2 {
				code = binary.BigEndian.Uint16(payload[:2])
				reason = string(payload[2:])
			}
			// Echo the close (best effort), then tear down.
			c.Close()
			return nil, &CloseError{Code: code, Reason: reason}
		case opText, opBinary:
			if msg != nil {
				return nil, c.protocolError("new data frame during fragmented message")
			}
			msg = payload
		case opContinuation:
			if msg == nil {
				return nil, c.protocolError("unexpected continuation frame")
			}
			if int64(len(msg))+int64(len(payload)) > c.maxMsg {
				return nil, c.tooBigError(int64(len(msg)) + int64(len(payload)))
			}
			msg = append(msg, payload...)
		}
		if fin {
			out := msg
			msg = nil
			if out != nil {
				return out, nil
			}
			// Zero-length message is valid; keep looping only if msg was nil
			// because of a control frame — control frames `continue` above,
			// so reaching here with out==nil means an empty data frame.
			return []byte{}, nil
		}
	}
}

func (c *WSConn) protocolError(msg string) error {
	c.closeWithCode(closeProtocol, msg)
	return fmt.Errorf("octobridge: websocket protocol error: %s", msg)
}

func (c *WSConn) tooBigError(n int64) error {
	c.closeWithCode(closeTooBig, "message too big")
	return fmt.Errorf("octobridge: websocket message of %d bytes exceeds limit %d", n, c.maxMsg)
}

// ---- writing ----

// writeFrame serializes one frame. Client frames are masked per RFC 6455.
func (c *WSConn) writeFrame(fin bool, opcode byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	select {
	case <-c.closed:
		return ErrConnClosed
	default:
	}

	length := len(payload)
	maskBit := byte(0)
	if c.isClient {
		maskBit = 0x80
	}

	hdr := make([]byte, 0, 14)
	b0 := opcode
	if fin {
		b0 |= 0x80
	}
	hdr = append(hdr, b0)
	switch {
	case length > 65535:
		hdr = append(hdr, 127|maskBit)
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(length))
		hdr = append(hdr, ext[:]...)
	case length > 125:
		hdr = append(hdr, 126|maskBit)
		var ext [2]byte
		binary.BigEndian.PutUint16(ext[:], uint16(length))
		hdr = append(hdr, ext[:]...)
	default:
		hdr = append(hdr, byte(length)|maskBit)
	}

	var body []byte
	if c.isClient {
		var key [4]byte
		if _, err := rand.Read(key[:]); err != nil {
			return fmt.Errorf("octobridge: mask key: %w", err)
		}
		hdr = append(hdr, key[:]...)
		body = make([]byte, length)
		copy(body, payload)
		applyMask(key, body)
	} else {
		body = payload
	}

	frame := append(hdr, body...)
	if _, err := c.conn.Write(frame); err != nil {
		return err
	}
	return nil
}

// WriteMessage writes a complete text message in a single frame.
func (c *WSConn) WriteMessage(data []byte) error {
	if int64(len(data)) > c.maxMsg {
		return fmt.Errorf("octobridge: outbound message of %d bytes exceeds limit %d", len(data), c.maxMsg)
	}
	return c.writeFrame(true, opText, data)
}

// Ping sends a control ping (payload ≤125 bytes).
func (c *WSConn) Ping() error { return c.writeFrame(true, opPing, nil) }

func applyMask(key [4]byte, data []byte) {
	for i := range data {
		data[i] ^= key[i&3]
	}
}

// ---- close ----

// Close performs a best-effort close handshake and tears down the connection.
func (c *WSConn) Close() error { return c.closeWithCode(closeNormal, "") }

func (c *WSConn) closeWithCode(code uint16, reason string) error {
	var err error
	c.closeOnce.Do(func() {
		// Best effort: short deadline, ignore write failure (peer may be gone).
		_ = c.conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
		payload := make([]byte, 2, 2+len(reason))
		binary.BigEndian.PutUint16(payload, code)
		if len(reason) > 123 {
			reason = reason[:123]
		}
		payload = append(payload, reason...)
		_ = c.writeFrameRaw(true, opClose, payload)
		err = c.conn.Close()
		close(c.closed)
	})
	return err
}

// writeFrameRaw is writeFrame without the closed-channel guard, used by the
// close path itself (where `closed` is not yet signaled but we must still
// emit the close frame).
func (c *WSConn) writeFrameRaw(fin bool, opcode byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	length := len(payload)
	maskBit := byte(0)
	if c.isClient {
		maskBit = 0x80
	}
	hdr := []byte{0x80 | opcode, byte(length) | maskBit} // close payloads are always ≤125
	if c.isClient {
		var key [4]byte
		if _, err := rand.Read(key[:]); err != nil {
			return err
		}
		hdr = append(hdr, key[:]...)
		body := make([]byte, length)
		copy(body, payload)
		applyMask(key, body)
		hdr = append(hdr, body...)
	} else {
		hdr = append(hdr, payload...)
	}
	_, err := c.conn.Write(hdr)
	return err
}

// ---- client handshake ----

// dialWS performs the opening handshake and returns a live WSConn.
func dialWS(ctx context.Context, rawURL string, extraHeaders http.Header, dialTimeout time.Duration, tlsCfg *tls.Config, maxMsg int64) (*WSConn, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("octobridge: bad ws url: %w", err)
	}
	var secure bool
	var defaultPort string
	switch u.Scheme {
	case "ws":
		secure, defaultPort = false, "80"
	case "wss":
		secure, defaultPort = true, "443"
	default:
		return nil, fmt.Errorf("octobridge: unsupported ws scheme %q", u.Scheme)
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		port = defaultPort
	}

	d := &net.Dialer{Timeout: dialTimeout}
	conn, err := d.DialContext(ctx, "tcp", net.JoinHostPort(host, port))
	if err != nil {
		return nil, fmt.Errorf("octobridge: dial %s: %w", u.Host, err)
	}
	_ = conn.SetDeadline(time.Now().Add(dialTimeout))

	if secure {
		cfg := tlsCfg
		if cfg == nil {
			cfg = &tls.Config{MinVersion: tls.VersionTLS12, ServerName: host}
		} else if cfg.ServerName == "" {
			cfg = cfg.Clone()
			cfg.ServerName = host
		}
		tc := tls.Client(conn, cfg)
		if err := tc.HandshakeContext(ctx); err != nil {
			_ = conn.Close()
			return nil, fmt.Errorf("octobridge: tls handshake: %w", err)
		}
		conn = tc
	}

	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("octobridge: ws key: %w", err)
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)

	var req strings.Builder
	fmt.Fprintf(&req, "GET %s HTTP/1.1\r\n", u.RequestURI())
	fmt.Fprintf(&req, "Host: %s\r\n", u.Host)
	req.WriteString("Upgrade: websocket\r\n")
	req.WriteString("Connection: Upgrade\r\n")
	fmt.Fprintf(&req, "Sec-WebSocket-Key: %s\r\n", key)
	req.WriteString("Sec-WebSocket-Version: 13\r\n")
	for name, values := range extraHeaders {
		for _, v := range values {
			fmt.Fprintf(&req, "%s: %s\r\n", name, v)
		}
	}
	req.WriteString("\r\n")

	if _, err := conn.Write([]byte(req.String())); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("octobridge: send handshake: %w", err)
	}

	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("octobridge: read handshake: %w", err)
	}
	status := resp.StatusCode
	upgrade := resp.Header.Get("Upgrade")
	accept := resp.Header.Get("Sec-WebSocket-Accept")
	// On 101 Switching Protocols the raw conn is now a WebSocket stream owned
	// by WSConn (via br). The response body must NOT be read or closed: doing
	// so could drain frame bytes already buffered in br. Drop the reference.
	resp.Body = nil

	if status != http.StatusSwitchingProtocols {
		_ = conn.Close()
		return nil, fmt.Errorf("octobridge: handshake failed: HTTP %d", status)
	}
	if !strings.EqualFold(upgrade, "websocket") {
		_ = conn.Close()
		return nil, fmt.Errorf("octobridge: handshake failed: missing Upgrade: websocket")
	}
	if accept != acceptKey(key) {
		_ = conn.Close()
		return nil, errors.New("octobridge: handshake failed: bad Sec-WebSocket-Accept")
	}

	_ = conn.SetDeadline(time.Time{}) // clear handshake deadline
	return newWSConn(conn, br, true, maxMsg), nil
}

func acceptKey(k string) string {
	h := sha1.Sum([]byte(k + websocketGUID)) // #nosec G401 - mandated by RFC 6455
	return base64.StdEncoding.EncodeToString(h[:])
}

// ---- server handshake (used by the mock server and tests) ----

// UpgradeWS completes the server side of the opening handshake, hijacking
// the HTTP connection. Exported so the bundled mock server (internal/testserver)
// and integration tests can speak the same protocol as the real adapter.
func UpgradeWS(w http.ResponseWriter, r *http.Request, maxMsg int64) (*WSConn, error) {
	if !headerContainsToken(r.Header.Get("Connection"), "upgrade") ||
		!strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		http.Error(w, "expected websocket upgrade", http.StatusBadRequest)
		return nil, errors.New("octobridge: not a websocket upgrade request")
	}
	if v := r.Header.Get("Sec-WebSocket-Version"); v != "13" {
		w.Header().Set("Sec-WebSocket-Version", "13")
		http.Error(w, "unsupported websocket version", http.StatusUpgradeRequired)
		return nil, fmt.Errorf("octobridge: unsupported ws version %q", v)
	}
	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" {
		http.Error(w, "missing Sec-WebSocket-Key", http.StatusBadRequest)
		return nil, errors.New("octobridge: missing Sec-WebSocket-Key")
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "server does not support hijacking", http.StatusInternalServerError)
		return nil, errors.New("octobridge: ResponseWriter is not an http.Hijacker")
	}
	conn, brw, err := hj.Hijack()
	if err != nil {
		return nil, fmt.Errorf("octobridge: hijack: %w", err)
	}
	resp := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + acceptKey(key) + "\r\n\r\n"
	if _, err := conn.Write([]byte(resp)); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("octobridge: write handshake: %w", err)
	}
	br := brw.Reader
	if br == nil {
		br = bufio.NewReader(conn)
	}
	return newWSConn(conn, br, false, maxMsg), nil
}

func headerContainsToken(header, token string) bool {
	for _, part := range strings.Split(header, ",") {
		if strings.EqualFold(strings.TrimSpace(part), token) {
			return true
		}
	}
	return false
}
