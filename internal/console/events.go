package console

import (
	"encoding/json"
	"net/http"
	"sync"
)

// eventBus is a minimal fan-out for Server-Sent Events. Subscribers get a
// buffered channel; a slow consumer drops old events rather than blocking the
// bridge's audit path (audit must never stall the tool pipeline).
type eventBus struct {
	mu     sync.Mutex
	nextID int
	subs   map[int]chan sseEvent
	closed bool
}

type sseEvent struct {
	Name string
	Data any
}

func newEventBus() *eventBus {
	return &eventBus{subs: make(map[int]chan sseEvent)}
}

func (b *eventBus) subscribe() chan sseEvent {
	ch := make(chan sseEvent, 64)
	b.mu.Lock()
	if b.closed {
		close(ch)
	} else {
		b.nextID++
		b.subs[b.nextID] = ch
	}
	b.mu.Unlock()
	return ch
}

func (b *eventBus) unsubscribe(ch chan sseEvent) {
	b.mu.Lock()
	delete(b.subs, b.nextIDFor(ch))
	b.mu.Unlock()
}

func (b *eventBus) nextIDFor(ch chan sseEvent) int {
	for id, c := range b.subs {
		if c == ch {
			return id
		}
	}
	return -1
}

func (b *eventBus) publish(name string, data any) {
	msg, err := json.Marshal(data)
	if err != nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	for _, ch := range b.subs {
		select {
		case ch <- sseEvent{Name: name, Data: json.RawMessage(msg)}:
		default: // buffer full: drop, never block the caller
		}
	}
}

func (b *eventBus) close() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return
	}
	b.closed = true
	for _, ch := range b.subs {
		close(ch)
	}
	b.subs = nil
}

// handleEvents serves GET /api/events as an SSE stream: event: status / audit.
func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Accel-Buffering", "no")

	ch := s.events.subscribe()
	defer s.events.unsubscribe(ch)

	// Initial state so a freshly opened page is correct before any event.
	if data, err := json.Marshal(s.st.snapshot()); err == nil {
		writeSSE(w, "status", data)
		fl.Flush()
	}
	heartbeat := newHeartbeat()
	defer heartbeat.Stop()

	for {
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.Tick():
			if _, err := writeSSE(w, "ping", nil); err != nil {
				return
			}
			fl.Flush()
		case ev, ok := <-ch:
			if !ok {
				return
			}
			data, err := json.Marshal(ev.Data)
			if err != nil {
				continue
			}
			if _, err := writeSSE(w, ev.Name, data); err != nil {
				return
			}
			fl.Flush()
		}
	}
}
