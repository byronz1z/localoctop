package localoctop

import (
	"bufio"
	"net"
	"testing"
	"time"
)

// wsPair creates a client/server WSConn pair over a real loopback TCP
// connection. Real sockets (unlike net.Pipe) have kernel buffers, so
// interleaved ping/pong/data traffic cannot deadlock the test.
func wsPair(t *testing.T, clientMax, serverMax int64) (client, server *WSConn) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	type acceptResult struct {
		conn net.Conn
		err  error
	}
	accepted := make(chan acceptResult, 1)
	go func() {
		c, err := ln.Accept()
		accepted <- acceptResult{c, err}
	}()

	cc, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	ar := <-accepted
	if ar.err != nil {
		t.Fatalf("accept: %v", ar.err)
	}

	client = newWSConn(cc, bufio.NewReader(cc), true, clientMax)
	server = newWSConn(ar.conn, bufio.NewReader(ar.conn), false, serverMax)
	t.Cleanup(func() { client.Close(); server.Close() })
	return client, server
}

func wsPairSym(t *testing.T, maxMsg int64) (*WSConn, *WSConn) {
	return wsPair(t, maxMsg, maxMsg)
}

func TestWS_RoundTripText(t *testing.T) {
	client, server := wsPairSym(t, 1<<20)
	msg := []byte(`{"id":1,"method":"list_directory"}`)

	go func() { _ = client.WriteMessage(msg) }()

	_ = server.SetReadDeadline(time.Now().Add(5 * time.Second))
	got, err := server.ReadMessage()
	if err != nil {
		t.Fatalf("server read: %v", err)
	}
	if string(got) != string(msg) {
		t.Fatalf("round-trip mismatch: got %q want %q", got, msg)
	}
}

func TestWS_Bidirectional(t *testing.T) {
	client, server := wsPairSym(t, 1<<20)

	go func() { _ = server.WriteMessage([]byte("response-a")) }()
	_ = client.SetReadDeadline(time.Now().Add(5 * time.Second))
	got, err := client.ReadMessage()
	if err != nil {
		t.Fatalf("client read: %v", err)
	}
	if string(got) != "response-a" {
		t.Fatalf("got %q", got)
	}

	go func() { _ = client.WriteMessage([]byte("request-b")) }()
	_ = server.SetReadDeadline(time.Now().Add(5 * time.Second))
	got, err = server.ReadMessage()
	if err != nil {
		t.Fatalf("server read: %v", err)
	}
	if string(got) != "request-b" {
		t.Fatalf("got %q", got)
	}
}

func TestWS_LargePayload16BitAnd64Bit(t *testing.T) {
	client, server := wsPairSym(t, 4<<20)
	// 200 bytes = 7-bit length; 70k = 16-bit extended; 200k = 64-bit extended.
	for _, size := range []int{200, 70_000, 200_000} {
		payload := make([]byte, size)
		for i := range payload {
			payload[i] = byte(i % 251)
		}
		go func(p []byte) { _ = client.WriteMessage(p) }(payload)
		_ = server.SetReadDeadline(time.Now().Add(10 * time.Second))
		got, err := server.ReadMessage()
		if err != nil {
			t.Fatalf("size %d: server read: %v", size, err)
		}
		if len(got) != size {
			t.Fatalf("size %d: got %d bytes", size, len(got))
		}
		for i := 0; i < len(got); i += 9973 {
			if got[i] != byte(i%251) {
				t.Fatalf("size %d: payload corrupted at %d", size, i)
			}
		}
	}
}

func TestWS_MaxMsgEnforced(t *testing.T) {
	// Client allowed to send big; server caps at 1KB -> server must error.
	client, server := wsPair(t, 1<<20, 1024)
	go func() { _ = client.WriteMessage(make([]byte, 4096)) }()
	_ = server.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := server.ReadMessage(); err == nil {
		t.Fatal("expected oversize message to fail on the receiving side")
	}
}

func TestWS_PingAutoPong(t *testing.T) {
	client, server := wsPairSym(t, 1<<20)
	// Server pings, then sends a real message. The client's ReadMessage must
	// auto-pong the ping (invisible to the caller) and return the message.
	go func() {
		_ = server.writeFrame(true, opPing, []byte("hb"))
		_ = server.WriteMessage([]byte("after-ping"))
	}()

	// Drain the auto-pong on the server side concurrently so neither direction
	// stalls, then verify the client got the data message.
	pongCh := make(chan bool, 1)
	go func() {
		_ = server.SetReadDeadline(time.Now().Add(5 * time.Second))
		fin, op, _, err := server.readFrame()
		pongCh <- err == nil && op == opPong && fin
	}()

	_ = client.SetReadDeadline(time.Now().Add(5 * time.Second))
	got, err := client.ReadMessage()
	if err != nil {
		t.Fatalf("client read after ping: %v", err)
	}
	if string(got) != "after-ping" {
		t.Fatalf("got %q", got)
	}
	if !<-pongCh {
		t.Fatal("server did not receive a well-formed auto-pong")
	}
}

func TestWS_CloseFrameSurfaces(t *testing.T) {
	client, server := wsPairSym(t, 1<<20)
	go func() { _ = server.closeWithCode(closeNormal, "bye") }()
	_ = client.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, err := client.ReadMessage()
	if !IsCloseError(err) {
		t.Fatalf("expected CloseError, got %v", err)
	}
	var ce *CloseError
	if asClose(err, &ce) && ce.Reason != "bye" {
		t.Fatalf("close reason not propagated: %q", ce.Reason)
	}
}

func asClose(err error, target **CloseError) bool {
	if ce, ok := err.(*CloseError); ok {
		*target = ce
		return true
	}
	return false
}

func TestWS_WriteAfterCloseFails(t *testing.T) {
	client, _ := wsPairSym(t, 1<<20)
	client.Close()
	if err := client.WriteMessage([]byte("x")); err == nil {
		t.Fatal("expected write after close to fail")
	}
}

func TestWS_FragmentedMessageReassembly(t *testing.T) {
	client, server := wsPairSym(t, 1<<20)
	go func() {
		// "hel" + "lo" as two frames of one text message.
		_ = client.writeFrame(false, opText, []byte("hel"))
		_ = client.writeFrame(true, opContinuation, []byte("lo"))
	}()
	_ = server.SetReadDeadline(time.Now().Add(5 * time.Second))
	got, err := server.ReadMessage()
	if err != nil {
		t.Fatalf("read fragmented: %v", err)
	}
	if string(got) != "hello" {
		t.Fatalf("fragment reassembly mismatch: %q", got)
	}
}

func TestWS_EmptyMessage(t *testing.T) {
	client, server := wsPairSym(t, 1<<20)
	go func() { _ = client.WriteMessage([]byte{}) }()
	_ = server.SetReadDeadline(time.Now().Add(5 * time.Second))
	got, err := server.ReadMessage()
	if err != nil {
		t.Fatalf("read empty: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("expected empty message, got %q", got)
	}
}

func TestWS_ServerRejectsUnmaskedClientFrame(t *testing.T) {
	// RFC 6455 §5.1: frames from a client MUST be masked. A server conn that
	// receives an unmasked frame must fail the connection.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	accepted := make(chan net.Conn, 1)
	go func() { c, _ := ln.Accept(); accepted <- c }()
	cc, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	srvConn := <-accepted

	// badWriter claims isClient=false, so it writes *unmasked* frames —
	// exactly what a misbehaving client would send.
	badWriter := newWSConn(cc, bufio.NewReader(cc), false, 1<<20)
	srv := newWSConn(srvConn, bufio.NewReader(srvConn), false, 1<<20)
	t.Cleanup(func() { badWriter.Close(); srv.Close() })

	go func() { _ = badWriter.WriteMessage([]byte("illegal")) }()
	_ = srv.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := srv.ReadMessage(); err == nil {
		t.Fatal("server accepted an unmasked client frame")
	}
}

func TestAcceptKey(t *testing.T) {
	// RFC 6455 §1.3 worked example.
	if got := acceptKey("dGhlIHNhbXBsZSBub25jZQ=="); got != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" {
		t.Fatalf("acceptKey mismatch: %s", got)
	}
}

func TestApplyMaskRoundTrip(t *testing.T) {
	key := [4]byte{0xde, 0xad, 0xbe, 0xef}
	data := []byte("mask me please, mask me again")
	orig := append([]byte(nil), data...)
	applyMask(key, data)
	if string(data) == string(orig) {
		t.Fatal("mask did not change payload")
	}
	applyMask(key, data) // XOR is its own inverse
	if string(data) != string(orig) {
		t.Fatalf("mask not reversible: %q", data)
	}
}
