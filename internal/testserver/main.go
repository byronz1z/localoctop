// Command testserver is the local integration partner for the localoctop (development-only)
// client. It emulates the localoctop adapter's WebSocket side:
//
//   - Accepts outbound client connections on /mcp/localoctop/ws?token=...
//   - Validates the token against a configured map (default: "dev-token")
//   - Expects a {"type":"register",...} frame right after the handshake
//   - Serves a tiny HTTP console on / that lets a human fire tool calls at
//     the connected bridge and see raw JSON responses
//   - POST /call {"token": "...", "method": "read_file", "params": {...}}
//     proxies a JSON-RPC-style call through the bridge and returns the reply
//
// Usage:
//
//	go run ./internal/testserver -addr 127.0.0.1:18443 -token dev-token
//
// Then start the bridge against ws://127.0.0.1:18443/mcp/localoctop/ws?token=dev-token
// and exercise it:
//
//	curl -s -X POST http://127.0.0.1:18443/call \
//	     -H 'Content-Type: application/json' \
//	     -d '{"token":"dev-token","method":"list_directory","params":{"path":"."}}'
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/byronz1z/localoctop"
)

// session is one connected bridge client.
type session struct {
	token     string
	clientID  string
	conn      *localoctop.WSConn
	connected time.Time

	mu      sync.Mutex
	nextID  int
	pending map[int]chan []byte
}

// server tracks sessions per token and proxies /call requests.
type server struct {
	log    *log.Logger
	mu     sync.RWMutex
	sess   map[string]*session // token -> latest session
	tokens map[string]bool
}

func main() {
	addr := flag.String("addr", "127.0.0.1:18443", "listen address")
	tokenCSV := flag.String("token", "dev-token", "comma-separated list of accepted client tokens")
	verbose := flag.Bool("v", false, "verbose logging")
	flag.Parse()

	logger := log.New(os.Stderr, "[testserver] ", log.LstdFlags)
	s := &server{
		log:    logger,
		sess:   map[string]*session{},
		tokens: map[string]bool{},
	}
	for _, t := range strings.Split(*tokenCSV, ",") {
		if t = strings.TrimSpace(t); t != "" {
			s.tokens[t] = true
		}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/mcp/localoctop/ws", s.handleWS)
	mux.HandleFunc("/call", s.handleCall)
	mux.HandleFunc("/sessions", s.handleSessions)
	mux.HandleFunc("/", s.handleIndex)

	httpSrv := &http.Server{Addr: *addr, Handler: logMiddleware(logger, *verbose, mux)}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = httpSrv.Shutdown(shutdownCtx)
	}()

	logger.Printf("mock adapter listening on http://%s (ws path: /mcp/localoctop/ws)", *addr)
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Fatalf("listen: %v", err)
	}
	logger.Printf("shutdown complete")
}

// handleWS accepts one bridge client, reads its register frame, and pumps
// messages until disconnect.
func (s *server) handleWS(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("token")
	if token == "" {
		// Also accept Authorization: Bearer for parity with the real adapter.
		if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
			token = strings.TrimPrefix(h, "Bearer ")
		}
	}
	if !s.tokens[token] {
		s.log.Printf("rejecting ws: bad token from %s", r.RemoteAddr)
		http.Error(w, "invalid token", http.StatusUnauthorized)
		return
	}

	conn, err := localoctop.UpgradeWS(w, r, 24<<20)
	if err != nil {
		s.log.Printf("ws upgrade failed: %v", err)
		return
	}

	sess := &session{token: token, conn: conn, connected: time.Now(), pending: map[int]chan []byte{}}

	// First frame must be the register frame.
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	raw, err := conn.ReadMessage()
	if err != nil {
		s.log.Printf("token=%s: no register frame: %v", token, err)
		conn.Close()
		return
	}
	var reg struct {
		Type     string `json:"type"`
		ClientID string `json:"client_id"`
		Version  string `json:"version"`
	}
	if err := json.Unmarshal(raw, &reg); err != nil || reg.Type != "register" {
		s.log.Printf("token=%s: bad register frame: %v", token, err)
		conn.Close()
		return
	}
	_ = conn.SetReadDeadline(time.Time{})
	sess.clientID = reg.ClientID

	// Replace any previous session for this token (reconnect semantics).
	s.mu.Lock()
	if old, ok := s.sess[token]; ok && old != sess {
		s.log.Printf("token=%s: replacing stale session %s", token, old.clientID)
		old.conn.Close()
	}
	s.sess[token] = sess
	s.mu.Unlock()
	s.log.Printf("token=%s: bridge registered client_id=%s version=%s from %s", token, reg.ClientID, reg.Version, r.RemoteAddr)

	// Read pump: deliver responses to waiting /call requests. App-level
	// pings ({"type":"ping","id":N}) are answered with a JSON pong like the
	// real adapter (bridge_ws.py) instead of being routed as responses.
	for {
		raw, err := conn.ReadMessage()
		if err != nil {
			s.log.Printf("token=%s client=%s: disconnected: %v", token, sess.clientID, err)
			break
		}
		var env struct {
			Type string          `json:"type"`
			ID   json.RawMessage `json:"id"`
		}
		if err := json.Unmarshal(raw, &env); err != nil {
			continue
		}
		if env.Type == "ping" {
			pong, _ := json.Marshal(map[string]any{"id": env.ID, "result": map[string]string{"type": "pong"}})
			if err := conn.WriteMessage(pong); err != nil {
				break
			}
			continue
		}
		var id int
		if err := json.Unmarshal(env.ID, &id); err != nil {
			continue
		}
		sess.mu.Lock()
		ch, ok := sess.pending[id]
		if ok {
			delete(sess.pending, id)
		}
		sess.mu.Unlock()
		if ok {
			ch <- raw
		} else {
			s.log.Printf("token=%s: unsolicited response id=%d", token, id)
		}
	}

	s.mu.Lock()
	if cur, ok := s.sess[token]; ok && cur == sess {
		delete(s.sess, token)
	}
	s.mu.Unlock()
	conn.Close()
}

// callRequest is the body of POST /call.
type callRequest struct {
	Token  string         `json:"token"`
	Method string         `json:"method"`
	Params map[string]any `json:"params"`
}

// handleCall proxies a tool call to the bridge registered under Token and
// returns its raw JSON response.
func (s *server) handleCall(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req callRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad JSON body: "+err.Error(), http.StatusBadRequest)
		return
	}
	s.mu.RLock()
	sess := s.sess[req.Token]
	s.mu.RUnlock()
	if sess == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": "no bridge connected for this token",
		})
		return
	}

	sess.mu.Lock()
	sess.nextID++
	id := sess.nextID
	ch := make(chan []byte, 1)
	sess.pending[id] = ch
	sess.mu.Unlock()

	frame, err := json.Marshal(map[string]any{"id": id, "method": req.Method, "params": req.Params})
	if err != nil {
		http.Error(w, "encode: "+err.Error(), http.StatusInternalServerError)
		return
	}
	if err := sess.conn.WriteMessage(frame); err != nil {
		sess.mu.Lock()
		delete(sess.pending, id)
		sess.mu.Unlock()
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": "bridge write failed: " + err.Error()})
		return
	}

	select {
	case raw := <-ch:
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(raw)
	case <-time.After(35 * time.Second):
		sess.mu.Lock()
		delete(sess.pending, id)
		sess.mu.Unlock()
		writeJSON(w, http.StatusGatewayTimeout, map[string]any{"error": "bridge did not respond within 35s"})
	case <-r.Context().Done():
		sess.mu.Lock()
		delete(sess.pending, id)
		sess.mu.Unlock()
	}
}

func (s *server) handleSessions(w http.ResponseWriter, _ *http.Request) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]map[string]any, 0, len(s.sess))
	for token, sess := range s.sess {
		out = append(out, map[string]any{
			"token":        token,
			"client_id":    sess.clientID,
			"connected_at": sess.connected.Format(time.RFC3339),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": out})
}

func (s *server) handleIndex(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	fmt.Fprint(w, `localoctop mock adapter
--------------------------
GET  /sessions   list connected bridge sessions
POST /call       {"token","method","params"} -> proxy a tool call to the bridge
WS   /mcp/localoctop/ws?token=...   bridge client endpoint
`)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func logMiddleware(logger *log.Logger, verbose bool, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		if verbose {
			logger.Printf("%s %s (%s)", r.Method, r.URL.Path, time.Since(start))
		}
	})
}
