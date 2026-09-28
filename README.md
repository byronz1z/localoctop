# localfsbridge — 本地文件访问客户端桥（Go）

员工本机运行的出站 WSS 客户端。它主动连接云端 `mcp-localfs` 适配器，把云端
AI 助手（经 Octop「自定义 MCP 连接器」）发来的文件工具调用，落地成对**白名单目录**
的只读访问，并记录本地审计日志。

- 模块名：`github.com/byronz1z/localfsbridge`
- **零第三方依赖**：内置最小 RFC 6455 WebSocket 实现，`go build ./...` 可完全离线编译
- 自包含：不 import 任何主工程包，不引用本目录外的路径

## 目录结构

```
localfsbridge/
├── go.mod                 # module github.com/byronz1z/localfsbridge（无 require）
├── config.go              # Config + 校验/默认值/白名单目录规范化
├── errors.go              # 线上错误码（与 Python 侧 protocol/errors 对齐）
├── protocol.go            # 线协议类型：Request/Response/RegisterFrame + 工具结果结构
├── pathguard.go           # ★安全核心：白名单 / 路径规范化 / .. 拒绝 / 符号链接逃逸防护
├── tools.go               # 4 个只读工具 + 2 个预留写工具（默认关）
├── audit.go               # 本地审计日志（JSON lines，带轮转）
├── logger.go              # 分级 Logger 接口 + stderr 实现
├── ws.go                  # 内置最小 WebSocket（客户端拨号 + 服务端 Upgrade）
├── client.go              # ★Bridge：出站连接、指数退避重连、请求分发
├── *_test.go              # 单元测试（路径穿越、大小上限、写开关、WS、端到端）
├── cmd/
│   ├── bridge/            # 独立运行器（联调用 / Wails 一行接入示例）
│   └── mockserver/        # mock 适配器：模拟云端 WS 端点 + /call 控制台
└── scripts/
    ├── integration.ps1    # Windows 联调脚本（build→test→mock→桥→断言）
    └── integration.sh     # Linux/CI 等价脚本
```

## 快速开始

```bash
# 编译（离线，无需联网下载依赖）
go build ./...

# 单元测试 + 竞态检测
go test ./...
go test -race ./...

# 跑 vet
go vet ./...
```

### 联调（mock 服务端）

一个终端起 mock 适配器：

```bash
go run ./cmd/mockserver -addr 127.0.0.1:18443 -token dev-token -v
```

另一个终端起桥客户端，白名单指向任意目录：

```bash
go run ./cmd/bridge \
  -server "ws://127.0.0.1:18443/mcp-localfs/ws" \
  -token  dev-token \
  -dir    "~/Documents/ZBSwork" \
  -audit  "./audit.log" \
  -v
```

用 mock 的 `/call` 控制台发工具调用：

```bash
curl -s -X POST http://127.0.0.1:18443/call \
  -H 'Content-Type: application/json' \
  -d '{"token":"dev-token","method":"list_directory","params":{"path":"."}}'

# 路径穿越会被拒绝（错误码 4001）
curl -s -X POST http://127.0.0.1:18443/call \
  -H 'Content-Type: application/json' \
  -d '{"token":"dev-token","method":"read_file","params":{"path":"../../etc/passwd"}}'
```

一键端到端联调（构建 + 测试 + mock + 桥 + 安全断言 + 重连）：

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\integration.ps1
```

```bash
# Linux / CI
bash scripts/integration.sh
```

## 在 ZBSwork（Wails）壳中接入

任务书要求主工程「仅一处启动调用」。在壳的 `main.go` 启动流程里加入：

```go
import "github.com/byronz1z/localfsbridge"

// ctx 为应用生命周期 context（Wails 的 OnShutdown 可 cancel）
cfg := localfsbridge.NewConfig()
cfg.ServerURL = appSettings.LocalfsServerURL          // wss://<server>/mcp-localfs/ws
cfg.Token     = appSettings.LocalfsToken              // 适配器为该用户签发的 token
cfg.AllowedDirs = appSettings.LocalfsDirs             // 设置页可配，默认 ~/Documents/ZBSwork
cfg.AllowWrite  = appSettings.LocalfsAllowWrite       // 默认 false
cfg.AuditLogPath = filepath.Join(userDataDir, "localfs-audit.log")
cfg.Logger = wailsLoggerAdapter{}                     // 可选：桥接到 Wails runtime logger

bridge, err := localfsbridge.New(cfg)
if err != nil { /* 配置错误，提示用户 */ }
go bridge.Run(ctx)   // ← 唯一的一行接入；阻塞直到 ctx 取消，自动重连
```

`Bridge` 是并发安全的；`Run` 在独立 goroutine 中维持连接并按指数退避重连
（1s→2s→…→60s 封顶），重连后自动重新注册。进程退出时 cancel `ctx` 或调用
`bridge.Shutdown()` 即可干净关闭。

## 安全模型（三层中的客户端层）

| 要求 | 实现 | 测试 |
|---|---|---|
| 白名单目录 | `Config.AllowedDirs`，所有路径必须落在某个根内 | `TestResolveAbsolute_RejectsOutside` |
| 拒绝 `..` 穿越 | `pathGuard` 词法 + 组件级校验 | `TestResolveRelative_RejectsDotDot` |
| 符号链接逃逸 | `EvalSymlinks` 解析真实路径后再次校验是否在根内 | `TestResolveSymlinkEscape` |
| 单文件大小上限 | 读 20MB / 写 10MB（可配） | `TestReadFile_SizeCap` / `TestWriteEnabled_SizeCap` |
| 本地审计日志 | JSON lines，allow/deny/error 全记录，带轮转 | `audit_test.go` |
| 写工具默认关 | `AllowWrite=false` 时 `write_file`/`create_directory` 返回 4004 | `TestWriteDisabledByDefault` |
| NUL/绝对/UNC 路径 | 显式拒绝 | `TestResolveRelative_RejectsNUL` 等 |

所有文件访问都必须经过 `pathGuard`；工具层没有其它触达文件系统的路径。

## 配置项（`Config`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `ServerURL` | — | `wss://.../mcp-localfs/ws`，接受 `https://`（自动转 `wss://`） |
| `Token` | — | 适配器签发的用户 token |
| `AllowedDirs` | — | 白名单根目录列表，支持 `~` 与相对路径（解析为绝对） |
| `AllowWrite` | `false` | 是否开启预留写工具 |
| `MaxReadBytes` | 20MB | 单文件读上限 |
| `MaxWriteBytes` | 10MB | 单次写上限 |
| `AuditLogPath` | 空 | 审计日志路径（空=仅回调，不落盘） |
| `RequestWait` | 30s | 单次工具调用超时 |
| `MinBackoff`/`MaxBackoff` | 1s / 60s | 指数退避区间 |
| `PingInterval`/`PongWait` | 25s / 60s | 保活心跳 |
| `Logger` | stderr | 分级日志接口 |
| `OnStatus`/`OnAudit` | nil | 连接状态 / 审计事件回调（供 UI 展示） |

## 错误码（与 Python 侧对齐）

`0` OK · `-32602` 参数错误 · `-32601` 方法未找到 · `-32000` 内部错误 ·
`4001` 路径不允许 · `4002` 不存在 · `4003` 超限 · `4004` 写禁用 · `4005` 超时
