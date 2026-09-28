package console

import (
	"fmt"
	"net/http"
	"time"
)

// writeSSE emits one SSE frame.
func writeSSE(w http.ResponseWriter, name string, data []byte) (int, error) {
	n, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", name, data)
	return n, err
}

// heartbeat is a 25s keepalive ticker for SSE streams (so proxies and
// browsers don't time an idle EventSource out). PingInterval matches the
// bridge's WebSocket keep-alive for symmetry.
type heartbeat struct {
	t *time.Ticker
}

func newHeartbeat() *heartbeat { return &heartbeat{t: time.NewTicker(25 * time.Second)} }

func (h *heartbeat) Tick() <-chan time.Time { return h.t.C }

func (h *heartbeat) Stop() { h.t.Stop() }
