# Octop Local Bridge

本地文件访问桥：运行在员工本机的**出站** WSS 客户端。它主动连接云端
`localoctop` 适配器，把云端 AI 助手（经 Octop「自定义 MCP 连接器」）发来的
文件工具调用，落地成对**白名单目录**的受控访问，并把每一次访问记入本地审计日志。

交付形态是**单个 EXE**：桥核心 + 内嵌本地 Web 控制台 + 系统托盘，同一进程，
`go build` 即得，无 node 构建链。

- 模块名：`github.com/byronz1z/localoctop`
- 许可证：Apache-2.0
- 版本：v0.2.2（预览期）

## 版本锁线

同仓库单 tag 双资产发布：客户端与服务端版本号永远一致，升级两侧一起升；
同 minor 版本协议互容承诺；协议破坏性变更才升 major。

## 是什么

```text
云端 AI 助手（Octop 会话）
   |  工具调用（MCP：streamable_http + Bearer）
   v
Octop「自定义 MCP 连接器」
   |  https://<server>/mcp/localoctop/
   v
localoctop 适配器（云端容器，本仓 adapter/）
   |  WebSocket（由本程序主动拨出，防火墙友好）
   |  wss://<server>/mcp/localoctop/ws + bridge_token
   v
Octop Local Bridge（本机，单 EXE）
   |  仅限白名单目录
   v
你的文件
```

本机不开任何入站服务端口（控制台只监听 `127.0.0.1`），桥**只向外拨号**；
断线自动指数退避重连（1s→2s→…→60s 封顶）。

## 接入 Octop（三步，开箱即用）

发布包自带 `examples/`（连接器模板 + 图文指南），详见
[`examples/README.md`](examples/README.md)：

1. **跑适配器容器**：服务器上用本仓 `adapter/`（克隆即得，或下载 Release 资产
   `localoctop-server-*.zip`）`docker build` 后运行，反代出
   `https://…/mcp/localoctop/` 与 `wss://…/mcp/localoctop/ws`；
2. **桥 EXE 引导页填地址**：白名单目录 + `wss://…/mcp/localoctop/ws` +
   **bridge_token** → 保存并连接；
3. **Octop 后台贴连接器配置**：把 `examples/octop-mcp-connector.example.json`
   贴入「连接器 → 自定义 MCP」（URL + **mcp_token** Bearer）→ 测试，
   列出 4 个只读工具即通。

> 两个令牌别混用：桥拨入用 **bridge_token**，Octop 连接器用 **mcp_token**，
> 由适配器分别签发（`POST /admin/tokens/issue`）。

## 快速开始

### 下载

从 [GitHub Releases](../../releases) 下载 `localoctop-vX.Y.Z-windows-amd64.zip`
（含 `sha256` 校验文件），解压得到 `localoctop-*.exe`。

> EXE 只由 GitHub 官方 CI（tag `v*` 触发的 windows-latest workflow）构建发布；
> 任何"本地编译的 EXE"都不是交付物。

### 首次运行

双击运行（或命令行 `localoctop-*.exe`）：

1. 系统托盘出现桥图标；浏览器自动打开控制台 `http://127.0.0.1:19880`；
2. 首次启动进入**引导页**：选择白名单目录 → 填写服务器地址（`wss://…/mcp/localoctop/ws`）
   与访问令牌 → 「保存并连接」；
3. 状态页显示「已连接」，审计活动开始实时滚动。

### 从源码构建（clone 即 build，无第三方构建链）

```bash
go build ./...          # 编译
go vet ./...            # 静态检查
go test ./...           # 单元测试

# 产出单 EXE（与 CI 发布同参数）
go build -ldflags "-s -w" -o dist/localoctop.exe ./cmd/bridge
```

Go 1.22+（CI 使用 1.23）。前端是手写单页 HTML + 原生 JS，经 `go:embed`
内嵌，**不需要** node/webpack 等任何前端构建工具。

## 控制台（四页）

| 页面 | 功能 |
|---|---|
| 状态 | 连接状态 / 重连次数 / 客户端 ID / 服务器 / 审计流实时追加（SSE） |
| 目录白名单 | 增加 / 删除 / 启停白名单目录；保存即重建桥连接 |
| 连接配置 | 服务器 URL / 令牌（mask 显示）/ 写开关 / 控制台端口；保存写配置文件并自动重连 |
| 关于 | 版本 / 控制台地址 / 配置文件与审计日志路径 |

控制台只绑定 `127.0.0.1`；令牌只保存在本机配置文件（0600），不进日志，
输入框默认 mask。

## 配置

配置文件位置（JSON）：

- Windows: `%AppData%\localoctop\config.json`
- Linux: `$XDG_CONFIG_HOME/localoctop/config.json`
- macOS: `$HOME/Library/Application Support/localoctop/config.json`

| 字段 | 默认 | 说明 |
|---|---|---|
| `server_url` | — | 适配器 WebSocket URL，`wss://…/mcp/localoctop/ws`（接受 `https://`，自动转 `wss://`） |
| `token` | — | 适配器签发的用户令牌 |
| `allowed_dirs` | — | 白名单目录列表，`[{path, enabled}]`；`enabled:false` 保留但不服务 |
| `allow_write` | `false` | 是否开启预留写工具（`write_file`/`create_directory`） |
| `console_port` | `19880` | 控制台首选端口；被占用时自动向上探测（最多 20 个） |
| `open_browser` | `true` | 启动时自动打开控制台页面 |
| `audit_log_path` | 数据目录下 `audit.jsonl` | 审计日志位置 |

### `--headless` 无人值守模式

不带控制台与托盘的纯桥模式，行为等价于早期 `cmd/bridge` CLI：

```bash
localoctop.exe --headless \
  -server "wss://octop.example.com/mcp/localoctop/ws" \
  -token  <token> \
  -dir    "D:/projects/docs" \
  -audit  "./audit.jsonl"
```

标志留空时自动回退读取配置文件；也支持环境变量
`LOCALOCTOP_SERVER_URL` / `LOCALOCTOP_TOKEN` / `LOCALOCTOP_DIRS` / `LOCALOCTOP_ALLOW_WRITE` / `LOCALOCTOP_AUDIT`。

## 安全模型

| 要求 | 实现 |
|---|---|
| 白名单目录 | 所有路径必须落在某个白名单根内（`pathguard.go`） |
| 拒绝 `..` 穿越 | 词法 + 组件级双重校验 |
| 符号链接逃逸 | `EvalSymlinks` 解析真实路径后二次校验 |
| NUL/UNC/绝对盘符注入 | 显式拒绝 |
| 单文件大小上限 | 读 20 MB / 写 10 MB（可配） |
| 写工具默认关 | `allow_write=false` 时写类调用返回 4004 |
| Bearer 认证 | 握手头 `Authorization: Bearer <token>` + register 帧 |
| 全量审计 | JSONL：allow/deny/error 全记录，带轮转（默认 8 MB） |
| 控制台隔离 | 仅 `127.0.0.1`；令牌不落日志 |

错误码：`0` OK · `-32602` 参数错误 · `-32601` 方法未找到 · `-32000` 内部错误 ·
`4001` 路径不允许 · `4002` 不存在 · `4003` 超限 · `4004` 写禁用 · `4005` 超时

## 仓库结构

```
├── config.go / pathguard.go / tools.go / audit.go / ws.go / client.go …
│                      # 桥核心（零第三方依赖的最小 RFC 6455 实现）
├── cmd/bridge/        # 产品入口：桌面模式（控制台+托盘）与 --headless
├── internal/appcfg/   # 用户配置文件读写（OS 配置目录，0600）
├── internal/console/  # 内嵌单页控制台 + JSON API + SSE
├── internal/tray/     # 系统托盘（fyne.io/systray，Windows 无 cgo）
├── internal/testserver/  # 开发/联调用 mock 适配器（非交付物，勿部署）
├── adapter/           # localoctop 云端适配器（Python，自包含，见其 README）
├── examples/          # Octop 连接器模板 + 三步接入指南（随发布包分发）
├── scripts/           # 端到端联调脚本（ps1 / sh）+ verify_e2e.ps1 三段验收
└── .github/workflows/ # push=build+test；tag v*=EXE zip+适配器 zip+sha256 发布
```

### 联调（开发者）

```powershell
# Windows 一键：构建 + 测试 + mock + 桥 + 安全断言 + 重连
powershell -ExecutionPolicy Bypass -File scripts\integration.ps1
```

```bash
bash scripts/integration.sh   # Linux/CI 等价
```

`internal/testserver` 是模拟云端适配器的 mock（`go run ./internal/testserver
-addr 127.0.0.1:18443 -token dev-token`），仅供本地联调与集成测试。

对**真实**适配器（`adapter/`）的三段端到端验收（起适配器 → 起桥 →
MCP 探测 + 双侧审计校验）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_e2e.ps1
```

## 致谢

本项目的控制台视觉风格（浅色、极简、卡片式、等宽字体代码块）与产品形态
（本地 Server/边界注册/连接管理/活动流）参照了
[cnPro/webcodex](https://github.com/cnPro/webcodex)（Apache-2.0）的公开设计；
仅借鉴其风格，未复制其代码。

## 许可证

[Apache License 2.0](LICENSE) · 漏洞报告见 [SECURITY.md](SECURITY.md)
