# T1 桥核心：心跳 pingLoop + 状态细节透出（0.6.0）

仓库：D:/SelfHosted/localoctop，分支 feature/v0.6.0（已建好，直接在此分支工作）
先读 AGENTS.md（AI 执行契约）再动手。

## 背景（已查证，不要重新调研）

- 70 秒掉线根因：**无人调用 conn.Ping()**。读侧已有 PongWait=60s 读超时（client.go readLoop 首行 SetIdleTimeout+SetReadDeadline），收到 opPing 自动回 pong（ws.go:202-203），重连退避已有（1s→60s）。缺的只是主动 ping 循环——空闲时零流量，读超时 60s 必触发断线（现场观察 70s 掉线与此吻合）。
- Config 已有字段：`PingInterval`（默认 25s）、`PongWait`（60s）、`MinBackoff`/`MaxBackoff`（config.go:25-30,77-80）。

## 任务

### 1. pingLoop（client.go）

`runOnce` 中连接注册成功后启动 `go b.pingLoop(ctx, conn)`：
- 每 `PingInterval` 调一次 `conn.Ping()`（写超时用现有 SetWriteDeadline 语义，ws.go 内部已处理）
- 发送失败（任何 error）：调 `conn.Close()` 触发 readLoop 退出 → 走既有重连路径
- ctx.Done / conn.Closed 时退出，goroutine 不泄漏（用 defer + select）
- 断线后 pingLoop 必须退出（下次 runOnce 重建），不得跨会话残留

### 2. 状态细节透出（client.go + config.go）

新增类型与回调（**保留旧 OnStatus 不动，老调用者零破坏**）：

```go
// client.go 或新文件 status.go
type StatusDetail struct {
    Connected        bool      // 当前是否在线
    ReconnectAttempt int       // 断线后第几次重连尝试（在线时为 0）
    LastError        string    // 最近一次失败原因（在线时为空）
    LastPingAt       time.Time // 最近一次成功发出的 ping（零值=从未）
    LastPongAt       time.Time // 最近一次收到 pong（零值=从未）
}

// config.go Config 新增字段：
OnStatusDetail func(StatusDetail) // 可为 nil；状态变化时调用
```

埋点位置：
- `runOnce` 拨号失败 / backoff sleep 前：ReconnectAttempt++、Connected=false、LastError=err → 上报
- 连接注册成功：Connected=true、ReconnectAttempt=0 → 上报
- pingLoop 每次成功 Ping 后更新 LastPingAt；readLoop 收到 pong 帧时更新 LastPongAt（ws.go:207 的 opPong 分支需要能触达 bridge——可在 WSConn 加 `OnPong func()` 钩子字段，newWSConn 后由 bridge 注入）
- 已有 `StatusDetail()` 读取方法（加锁读快照），供桌面端主动拉取

### 3. 测试（client_test.go 或新增 ping_test.go）

- pingLoop 空闲保活：起 testserver，桥连上后静置 > PingInterval*2，断言连接未断（对照组：现有无 ping 行为会超时）
- ping 目标不可达/写失败 → 连接关闭并重连
- StatusDetail 序列：断线→attempt 递增→重连成功→attempt 归零
- 全部跑 `go test ./... -count=1`

## 质量门（必须全过并附原文）

GOROOT=D:/tmp/go GOPATH=D:/tmp/gopath GOTMPDIR=D:/tmp/gotmp，PATH 前置 /d/tmp/go/bin:/d/tmp/gopath/bin

```
go build ./... && go vet ./... && go test ./... -count=1
```

（server/ 是 Python 目录、desktop/ 是另一 module，`./...` 不会碰它们；**禁止改动 desktop/ 与 server/ 下任何文件**）

## 交付

git add 改动文件（仅根模块）+ commit（中文信息，前缀 `feat(bridge):`），禁止 push。
报告：改动文件清单、pingLoop 时序说明、测试输出原文、git log -1。
