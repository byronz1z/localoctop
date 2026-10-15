package main

// T12: friendlyReason self-tests — every case uses error text the
// employee-side core actually produces (ws.go dialWS / client.go Run),
// not invented strings.

import (
	"strings"
	"testing"
)

func TestT12FriendlyReason(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"empty means nothing to say", "", ""},

		// 令牌不对或已失效：握手被服务器 401/403 拒（ws.go: handshake failed: HTTP %d）
		{"401 bad token", "localoctop: handshake failed: HTTP 401", "令牌不对或已失效"},
		{"403 forbidden", "localoctop: handshake failed: HTTP 403", "令牌不对或已失效"},

		// 证书有问题：TLS 握手 x509 失败（ws.go: localoctop: tls handshake: %w）
		{"x509 unknown authority",
			`localoctop: tls handshake: x509: certificate signed by unknown authority`,
			"安全证书有问题"},
		{"x509 expired",
			`localoctop: tls handshake: x509: certificate has expired or is not yet valid`,
			"安全证书有问题"},

		// 连不上服务器：net dial 的真实失败文字（ws.go: localoctop: dial %s: %w）
		{"no such host",
			"localoctop: dial octop.example.com:443: dial tcp: lookup octop.example.com: no such host",
			"找不到这个服务器地址"},
		{"connection refused",
			"localoctop: dial 127.0.0.1:1: dial tcp 127.0.0.1:1: connectex: No connection could be made because the target machine actively refused it.",
			"连不上服务器（对方拒绝连接）"},
		{"i/o timeout",
			"localoctop: dial 10.0.0.1:443: dial tcp 10.0.0.1:443: i/o timeout",
			"超时"},
		{"other dial failure",
			"localoctop: dial 192.168.1.5:443: dial tcp 192.168.1.5:443: connectex: A socket operation was attempted to an unreachable network.",
			"连不上服务器"},

		// 其他未知情况：带原文简短部分
		{"unknown keeps a short prefix",
			"localoctop: handshake failed: HTTP 500",
			"连接失败，原因：localoctop: handshake failed: HTTP 500"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := friendlyReason(c.in)
			if c.in == "" {
				if got != "" {
					t.Fatalf("friendlyReason(\"\") = %q, want \"\"", got)
				}
				return
			}
			if !strings.Contains(got, c.want) {
				t.Fatalf("friendlyReason(%q) = %q, want containing %q", c.in, got, c.want)
			}
			// The card sub-line must never show raw technical text beyond the
			// unknown-case short prefix: token/cert/dial cases are all-Chinese.
			if strings.Contains(got, "x509") || strings.Contains(got, "dial tcp") ||
				strings.Contains(got, "HTTP 401") || strings.Contains(got, "HTTP 403") {
				t.Fatalf("raw technical text leaked into friendly copy: %q", got)
			}
		})
	}
}

// TestT12FriendlyReasonTruncates: the unknown case caps the carried-over
// original text so a huge server message cannot blow up the card.
func TestT12FriendlyReasonTruncates(t *testing.T) {
	long := "something odd: " + strings.Repeat("很", 200)
	got := friendlyReason(long)
	if !strings.HasPrefix(got, "连接失败，原因：") {
		t.Fatalf("unknown error lost its prefix: %q", got)
	}
	if len([]rune(got)) > len([]rune("连接失败，原因："))+81 { // 80 + ellipsis
		t.Fatalf("unknown error not truncated: %d runes", len([]rune(got)))
	}
	if !strings.HasSuffix(got, "…") {
		t.Fatalf("truncated text should end with ellipsis: %q", got)
	}
}
