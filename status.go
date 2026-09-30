package localoctop

import (
	"sync"
	"time"
)

// StatusDetail is a rich snapshot of the bridge's connection state, for
// surfaces that want more than the binary up/down of OnStatus (e.g. the
// desktop tray: "重连中 第 3 次" vs a plain dot). Read it via
// Bridge.StatusDetail() or subscribe via Config.OnStatusDetail.
type StatusDetail struct {
	Connected        bool      // 当前是否在线
	ReconnectAttempt int       // 断线后第几次重连尝试（在线时为 0）
	LastError        string    // 最近一次失败原因（在线时为空）
	LastPingAt       time.Time // 最近一次成功发出的 ping（零值=从未）
	LastPongAt       time.Time // 最近一次收到 pong（零值=从未）
}

// statusTracker holds the fields behind StatusDetail. All mutations funnel
// through update(), which snapshots, diffs against the last reported value
// under one lock, and fires OnStatusDetail outside the lock when anything
// changed (so callbacks can safely call back into StatusDetail()).
type statusTracker struct {
	mu      sync.Mutex
	detail  StatusDetail
	last    StatusDetail // last value passed to OnStatusDetail
	onEvent func(StatusDetail)
}

// update applies fn to the tracked detail and reports the new snapshot if it
// differs from the last one surfaced to the callback.
func (s *statusTracker) update(fn func(*StatusDetail)) {
	s.mu.Lock()
	fn(&s.detail)
	cur := s.detail
	changed := cur != s.last
	s.last = cur
	cb := s.onEvent
	s.mu.Unlock()
	if changed && cb != nil {
		cb(cur)
	}
}

// snapshot returns the current detail.
func (s *statusTracker) snapshot() StatusDetail {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.detail
}
