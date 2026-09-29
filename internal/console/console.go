// Package console is the embedded local web console: a single-page HTML app
// served from 127.0.0.1 only, plus the JSON API and SSE event stream the page
// talks to. It never binds anything but the loopback interface.
package console

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"sync"

	"github.com/byronz1z/octop-local-bridge"
	"github.com/byronz1z/octop-local-bridge/internal/appcfg"
)

// Status is the live snapshot the status page renders. It is fed by the
// runtime wiring (main) from the bridge's OnStatus/OnAudit callbacks.
type Status struct {
	Connected   bool   `json:"connected"`
	LastError   string `json:"last_error,omitempty"`
	Reconnects  int    `json:"reconnects"`
	ClientID    string `json:"client_id"`
	ServerURL   string `json:"server_url"`
	LastAuditTS string `json:"last_audit_ts,omitempty"`
}

// status is the mutex-guarded runtime holder behind Status snapshots. The
// mutex lives here, not on Status, so returning snapshots never copies a
// lock (go vet copylocks).
type status struct {
	mu  sync.Mutex
	cur Status
}

func (s *status) snapshot() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cur
}

func (s *status) update(fn func(*Status)) {
	s.mu.Lock()
	fn(&s.cur)
	s.mu.Unlock()
}

// Server owns the HTTP listener and the console's view of the app.
type Server struct {
	cfg    *appcfg.File // guarded by cfgMu; the console reads a copy
	cfgMu  sync.RWMutex
	about  About // guarded by cfgMu
	st     *status
	events *eventBus
	apply  func(appcfg.File) error // validates + persists + rebuilds bridge
	mux    *http.ServeMux
}

// About is the static metadata the about page renders.
type About struct {
	Version    string `json:"version"`
	ConsoleURL string `json:"console_url"`
	ConfigPath string `json:"config_path"`
	AuditPath  string `json:"audit_path"`
}

// New builds a console Server. apply is called on every settings save; it must
// validate the config, persist it, and swap the running bridge. It runs on an
// HTTP handler goroutine.
func New(cfg appcfg.File, apply func(appcfg.File) error) *Server {
	s := &Server{
		st:     &status{},
		events: newEventBus(),
		apply:  apply,
	}
	s.SetConfig(cfg)
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/api/config", s.handleConfig)
	mux.HandleFunc("/api/status", s.handleStatus)
	mux.HandleFunc("/api/about", s.handleAbout)
	mux.HandleFunc("/api/events", s.handleEvents)
	mux.HandleFunc("/api/browse", s.handleBrowse)
	mux.HandleFunc("/api/commondirs", s.handleCommonDirs)
	s.mux = mux
	return s
}

// SetConfig swaps the console's copy of the settings (called by main after a
// bridge rebuild so GET /api/config reflects what is actually running).
func (s *Server) SetConfig(cfg appcfg.File) {
	s.cfgMu.Lock()
	s.cfg = &cfg
	s.cfgMu.Unlock()
}

// SetAbout records the runtime metadata shown on the about page. The console
// URL is only known after Listen, so main calls this once at startup.
func (s *Server) SetAbout(a About) {
	s.cfgMu.Lock()
	s.about = a
	s.cfgMu.Unlock()
}

func (s *Server) aboutSnapshot() About {
	s.cfgMu.RLock()
	defer s.cfgMu.RUnlock()
	return s.about
}

func (s *Server) config() appcfg.File {
	s.cfgMu.RLock()
	defer s.cfgMu.RUnlock()
	return *s.cfg
}

// Listen binds 127.0.0.1:<preferred>, probing upward on conflict up to
// rangeLimit ports, and returns the bound address. Binding anything other
// than the loopback interface is a security violation (task constraint).
func Listen(preferred, rangeLimit int) (net.Listener, error) {
	if preferred <= 0 {
		preferred = appcfg.DefaultConsolePort
	}
	if rangeLimit <= 0 {
		rangeLimit = appcfg.ConsolePortRange
	}
	var lastErr error
	for p := preferred; p < preferred+rangeLimit; p++ {
		ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(p)))
		if err == nil {
			return ln, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("console: no free port in 127.0.0.1:%d-%d: %w", preferred, preferred+rangeLimit-1, lastErr)
}

// Serve blocks serving the console on ln.
func (s *Server) Serve(ln net.Listener) error {
	return http.Serve(ln, s.mux)
}

// Addr is a helper for logging/notification: the http:// URL of the console.
func Addr(ln net.Listener) string { return "http://" + ln.Addr().String() }

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	data := consoleHTML
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Frame-Options", "DENY")
	_, _ = w.Write(data)
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.st.snapshot())
}

func (s *Server) handleAbout(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.aboutSnapshot())
}

// configView is what GET /api/config returns. The token is included — the
// console is loopback-only and the settings page needs to show the current
// value in its (masked) input.
type configView struct {
	ServerURL     string                `json:"server_url"`
	Token         string                `json:"token"`
	AllowedDirs   []appcfg.Dir          `json:"allowed_dirs"`
	AllowWrite    bool                  `json:"allow_write"`
	ConsolePort   int                   `json:"console_port"`
	OpenBrowser   bool                  `json:"open_browser"`
	RecentServers []appcfg.RecentServer `json:"recent_servers"`
	Configured    bool                  `json:"configured"` // false = show onboarding
}

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		c := s.config()
		writeJSON(w, http.StatusOK, configView{
			ServerURL:     c.ServerURL,
			Token:         c.Token,
			AllowedDirs:   c.AllowedDirs,
			AllowWrite:    c.AllowWrite,
			ConsolePort:   c.ConsolePort,
			OpenBrowser:   c.OpenBrowser,
			RecentServers: c.RecentServers,
			Configured:    c.ServerURL != "" && c.Token != "" && len(c.EnabledDirs()) > 0,
		})
	case http.MethodPost:
		var in configView
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
			return
		}
		cfg := s.config() // preserve AuditLogPath and any field the UI omits
		cfg.ServerURL = in.ServerURL
		cfg.Token = in.Token
		cfg.AllowedDirs = in.AllowedDirs
		cfg.AllowWrite = in.AllowWrite
		cfg.ConsolePort = in.ConsolePort
		cfg.OpenBrowser = in.OpenBrowser
		// Remember this connection (deduped, capped) so the console can offer
		// it in the server dropdown next time.
		cfg.RememberServer(in.ServerURL, in.Token)
		// apply validates via octobridge.Config.Validate, persists, and
		// rebuilds the bridge; the returned error is user-actionable.
		if err := s.apply(cfg); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		s.SetConfig(cfg)
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	default:
		w.Header().Set("Allow", "GET, POST")
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
	}
}

// handleBrowse opens the native directory picker and returns the chosen
// path. Serialized: at most one dialog at a time — a concurrent request gets
// 409 so the UI can tell the user instead of opening a second window.
func (s *Server) handleBrowse(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET")
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if !browseMu.TryLock() {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "a directory picker is already open"})
		return
	}
	defer browseMu.Unlock()

	ctx, cancel := context.WithTimeout(r.Context(), BrowseTimeout)
	defer cancel()
	path, ok, err := currentPicker()(ctx)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"selected": ok, "path": path})
}

// handleCommonDirs lists the frequently-used locations that exist on this
// machine, for one-click whitelist adding.
func (s *Server) handleCommonDirs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET")
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	dirs := commonDirs()
	if dirs == nil {
		dirs = []CommonDir{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"dirs": dirs})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// SetStatusBase fills the identity fields of the status snapshot; main calls
// it each time a new bridge is built so the status page shows the live
// client id and server URL.
func (s *Server) SetStatusBase(clientID, serverURL string) {
	s.st.update(func(x *Status) {
		x.ClientID = clientID
		x.ServerURL = serverURL
	})
}

// PushStatus is called by main from the bridge's OnStatus callback.
func (s *Server) PushStatus(connected bool, errMsg string) {
	s.st.update(func(x *Status) {
		x.Connected = connected
		if errMsg != "" {
			x.LastError = errMsg
		}
		if !connected {
			x.Reconnects++
		} else {
			x.LastError = ""
		}
	})
	s.events.publish("status", s.st.snapshot())
}

// PushAudit is called by main from the bridge's OnAudit callback.
func (s *Server) PushAudit(ev octobridge.AuditEvent) {
	s.st.update(func(x *Status) {
		x.LastAuditTS = ev.Time
	})
	s.events.publish("audit", ev)
}

// Shutdown releases SSE subscribers so Serve can return.
func (s *Server) Shutdown() {
	s.events.close()
}
