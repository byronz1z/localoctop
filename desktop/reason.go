// reason.go translates the bridge core's technical LastError strings into
// plain Chinese the status card can show directly (T12): the raw text stays
// in the hover tooltip; the card's sub-line tells the user WHY the link is
// down without mechanism talk.
//
// The patterns below match only error text the employee-side core actually
// produces (ws.go dialWS / client.go Run):
//
//	localoctop: handshake failed: HTTP 401|403        (ws.go, dialWS)
//	localoctop: dial <host>: dial tcp ...: connectex: ...  / i/o timeout
//	... no such host                                   (ws.go, net dial)
//	localoctop: tls handshake: x509: ...               (ws.go, dialWS)
//	localoctop: peer closed websocket (code=4000 reason="...")  (ws.go, parked)
package main

import "strings"

// friendlyReason maps a raw StatusDetail.LastError to one plain-language
// sentence for the status card. "" in → "" out (nothing to say when the
// link is fine or just starting). Unknown errors surface a short prefix of
// the original so the card never shows a bare "连接失败" with no clue.
func friendlyReason(lastError string) string {
	e := strings.TrimSpace(lastError)
	if e == "" {
		return ""
	}
	low := strings.ToLower(e)

	switch {
	// 已在别处登录：同令牌在另一台设备接管，服务器以 4000 关闭本机连接
	// （ws.go CloseError.Error() 的真实文字含 code=4000）。
	case strings.Contains(low, "code=4000"):
		return "这个令牌已在另一台电脑上登录，本机已暂停。"

	// 刚保存设置 / 手动点了连接：桥刚重建，还没有任何成败记录。
	// (handled by callers passing "" — see rebuildLocked)

	// 令牌不对或已失效：服务器在握手阶段拒绝（401/403 一类）。
	case strings.Contains(low, "handshake failed: http 401"),
		strings.Contains(low, "handshake failed: http 403"):
		return "令牌不对或已失效，服务器拒绝了连接；请找管理员重新获取令牌。"

	// 证书有问题：TLS 握手阶段的 x509 校验失败。
	case strings.Contains(low, "tls handshake") && strings.Contains(low, "x509"),
		strings.Contains(low, "x509:"):
		return "服务器的安全证书有问题，连接被拦下；请检查服务器地址是否正确，或联系管理员。"

	// 连不上服务器：域名解析失败、连接被拒、超时、网络不可达。
	case strings.Contains(low, "no such host"):
		return "找不到这个服务器地址；请检查设置里的服务器地址有没有填错。"
	case strings.Contains(low, "connection refused"),
		strings.Contains(low, "no connection could be made"):
		return "连不上服务器（对方拒绝连接）；服务器可能没在运行，或地址/端口填错了。"
	case strings.Contains(low, "i/o timeout"),
		strings.Contains(low, "deadline exceeded"),
		strings.Contains(low, "timeout"):
		return "连接服务器超时；请检查本机网络，或确认服务器地址是否正确。"
	case strings.Contains(low, "dial tcp"),
		strings.Contains(low, "localoctop: dial"):
		return "连不上服务器；请检查本机网络和设置里的服务器地址。"
	}

	// 其他未知情况：带上原文的简短部分，方便转述给管理员。
	return "连接失败，原因：" + shortReason(e)
}

// shortReason trims the raw error for inline display: first line, capped.
func shortReason(e string) string {
	if i := strings.IndexAny(e, "\r\n"); i >= 0 {
		e = e[:i]
	}
	const max = 80
	r := []rune(e)
	if len(r) > max {
		e = string(r[:max]) + "…"
	}
	return e
}
