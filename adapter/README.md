# localoctop 适配器（Python / FastAPI）

独立容器，部署在云端。两个职责：

1. **出站桥会话管理**：接收员工本机 `localoctop`（Go，本仓根目录）
   客户端主动拨入的 WSS 连接（`/mcp/localoctop/ws`），按 bridge_token 把每个用户
   绑定到自己的桥会话（NAT 友好，无需内网穿透）。
2. **对外 MCP 端点**：暴露 `streamable_http` 的 MCP 接口（`/mcp/localoctop/`），
   供 Octop「自定义 MCP 连接器」直接接入（`initialize` / `tools/list` /
   `tools/call`，Bearer 认证）。**上游零改动。**

- 自包含：不 import Octop 任何包，不引用本目录外的路径。
- 首期只读：`list_directory` / `read_file` / `search_files` / `get_file_info`；
  `write_file` / `create_directory` 预留，配置开关默认关。

## 目录结构

```
adapter/
├── requirements.txt           # fastapi / uvicorn / mcp + 测试依赖（范围 pin）
├── requirements-dev.txt       # 测试依赖的显式清单
├── pytest.ini                 # 测试配置（asyncio_mode=auto）
├── Dockerfile                 # 非 root、自带 healthcheck 的独立镜像（python:3.12-slim）
├── docker-compose.example.yml # 追加到现有 compose 的 service 片段
├── localoctop/
│   ├── config.py              # 环境驱动配置 + 双令牌认证（mcp / bridge）
│   ├── tokens.py              # 令牌存储：只留 SHA-256 哈希，明文只出示一次
│   ├── errors.py              # 错误码（与桥侧 Go errors.go 对齐）
│   ├── protocol.py            # MCP 工具目录 + JSON-RPC 封套
│   ├── sessions.py            # ★桥会话注册表：每用户隔离 + 重连替换 + 超时
│   ├── bridge_ws.py           # ★/mcp/localoctop/ws：客户端拨入端点
│   ├── tools.py               # tools/call 扇出 + MCP content 渲染
│   ├── mcp_app.py             # ★/mcp/localoctop/：MCP streamable_http 端点（官方 mcp SDK）
│   ├── audit.py               # 服务端调用审计（JSON lines，带轮转）
│   ├── app.py                 # FastAPI 装配 + /healthz + /admin/*
│   └── main.py                # uvicorn 入口
├── tests/                     # pytest（端点/会话/WS/配置/审计/协议 + 跨栈联调）
└── scripts/
    ├── verify.py              # 无 pytest 时的最小自验
    └── e2e_probe.py           # e2e 第三段：按 Octop 连接器行为做 MCP 探测
```

## 与桥的协议契约（以本仓 Go 源码为既成事实）

适配器拨入面与 `protocol.go` / `client.go` / `errors.go` 逐字段对齐：

| 契约点 | 桥侧（Go） | 适配器（Python） |
|---|---|---|
| WS 端点 | `ws(s)://host/mcp/localoctop/ws` | `ws_path` 默认 `/mcp/localoctop/ws` |
| 认证 | 握手头 `Authorization: Bearer <bridge_token>` | `authenticate_bridge()`（只认 bridge 命名空间） |
| 首帧 | `{"type":"register", token, client_id, hostname, allowed_dirs, write_enabled, version}` | 首帧必须 register，否则 4403；token 与握手不一致 4401 |
| 请求/响应 | `{id, method, params}` / `{id, result|error}` | `SessionRegistry.call` / `deliver` |
| 方法名 | 6 个工具方法 1:1 | `protocol.py` 同名常量 |
| 错误码 | 0 / -32602 / -32601 / -32000 / 4001–4005 | `errors.py` 同值（另加纯服务端 4101/4102） |
| 活性 | 25s RFC6455 协议级 ping | uvicorn 自动回 pong；120s 空闲才断（数据帧刷新） |

注：适配器另保留 JSON 控制帧 `{"type":"ping"}` 的兼容分支（对正式桥是死代码，
桥只发协议级 ping），以及 `?token=` 查询参数认证（桥不使用）——均为兼容面，
不构成安全削弱。

## 本地运行

```bash
cd adapter
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 配置：mcp 令牌（Octop 连接器 Bearer）与 bridge 令牌（客户端拨入）分开签发
export LOCALOCTOP_MCP_TOKENS="alice:tok-alice"                 # mcp 方向
export LOCALOCTOP_CLIENT_TOKENS="alice:btok-alice"         # bridge 方向
export LOCALOCTOP_PORT=8080
export LOCALOCTOP_AUDIT_LOG="./logs/calls.jsonl"

python -m localoctop.main
# → http://127.0.0.1:8080/healthz
```

> 只设 `LOCALOCTOP_MCP_TOKENS` 时两个方向共用同一组令牌（单令牌兼容模式），
> 生产建议分开签发、独立轮换。运行时签发：`POST /admin/tokens/issue`
> （body `{"user_id": "...", "kind": "mcp"|"bridge"}`，仅回环或
> `LOCALOCTOP_ADMIN_TOKEN` 可调）。

### 验证 MCP 握手

```bash
# initialize
curl -s http://127.0.0.1:8080/mcp/localoctop/ \
  -H 'Authorization: Bearer tok-alice' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'

# tools/list → 返回 4 个只读工具（写开关关闭时）
curl -s http://127.0.0.1:8080/mcp/localoctop/ \
  -H 'Authorization: Bearer tok-alice' -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: <上一步响应头的 mcp-session-id>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

## 测试

```bash
pip install -r requirements.txt        # 含 pytest / pytest-asyncio / httpx
pytest                                 # 全部单元测试
pytest -q tests/test_mcp_endpoint.py   # 只跑 MCP 端点

# 无 pytest 环境的最小自验（导入 + initialize + tools/list 断言）
python scripts/verify.py
```

> `tests/test_cross_stack.py` 会尝试构建本仓根目录的真实 Go 桥二进制
> （`go build ./cmd/bridge`，以 `-headless` 运行）跑端到端联调；
> 当本机没有 `go` 工具链时自动跳过，不影响其余测试。

### 端到端联调（仓库根目录）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_e2e.ps1
```

三段全真实进程：起适配器 → 起 `dist/localoctop.exe --headless` 拨入 →
按 Octop 自定义 MCP 连接器行为探测（`initialize` / `tools/list` /
`tools/call`，含穿越拒绝与写门控断言）→ 校验**两侧**审计日志
（桥 JSONL + 适配器 JSONL 均有 allow/deny 记录）。

## Docker 部署（独立容器，不进 octop 主镜像）

```bash
docker build -t localoctop-server:1.0.0 .
docker run -d --name localoctop-server \
  -p 127.0.0.1:8080:8080 \
  -e LOCALOCTOP_MCP_TOKENS="alice:$(openssl rand -hex 24)" \
  -e LOCALOCTOP_CLIENT_TOKENS="alice:$(openssl rand -hex 24)" \
  -e LOCALOCTOP_AUDIT_LOG=/app/logs/calls.jsonl \
  -v localoctop-audit:/app/logs \
  localoctop-server:1.0.0
```

或把 `docker-compose.example.yml` 里的 `localoctop-server` service 追加进现有
compose。放在反向代理（Caddy/Nginx/Traefik）之后，对外提供
`https://<server>/mcp/localoctop/` 与 `wss://<server>/mcp/localoctop/ws`，
并终结 TLS。

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOCALOCTOP_MCP_TOKENS` | 空 | mcp 令牌（Octop 连接器 Bearer）`user:token,...` |
| `LOCALOCTOP_CLIENT_TOKENS` | 空 | bridge 令牌（客户端拨入）`user:token,...`；未设时沿用 `LOCALOCTOP_MCP_TOKENS`（兼容模式） |
| `LOCALOCTOP_ALLOW_WRITE` | `false` | 写工具全局开关（默认关） |
| `LOCALOCTOP_WRITE_ALLOWLIST` | 空 | 逗号分隔用户白名单：即使全局关也允许其写（灰度用） |
| `LOCALOCTOP_DISABLED` | `false` | 管理员一键停用（所有 tools/call 拒绝） |
| `LOCALOCTOP_MAX_READ_BYTES` | 20MB | 读上限（服务端二次校验） |
| `LOCALOCTOP_MAX_WRITE_BYTES` | 10MB | 写上限 |
| `LOCALOCTOP_BRIDGE_TIMEOUT` | 18 | 桥往返超时（秒；预算：工具≤15 / 桥≤18 / 整体≤20） |
| `LOCALOCTOP_IDLE_TIMEOUT` | 120 | 桥无流量多久后断开（秒） |
| `LOCALOCTOP_AUDIT_LOG` | `logs/calls.jsonl` | 服务端审计日志路径 |
| `LOCALOCTOP_LOG_LEVEL` | `INFO` | 日志级别 |
| `LOCALOCTOP_HOST` / `LOCALOCTOP_PORT` | `0.0.0.0` / `8080` | 监听地址 |
| `LOCALOCTOP_MCP_PATH` | `/mcp/localoctop/` | MCP 端点路径 |
| `LOCALOCTOP_WS_PATH` | `/mcp/localoctop/ws` | 桥 WS 端点路径 |
| `LOCALOCTOP_ADMIN_TOKEN` | 空 | `/admin/*` 的管理令牌（空则仅回环可访问） |

## 与 Octop「自定义 MCP 连接器」对接

Octop `src/octop/infra/connectors/custom_mcp.py` 支持 `transport=streamable_http`
+ Bearer（经 `headers.Authorization`）。录入字段（`normalize_server_spec`）：
`transport` / `url` / `headers` / `display_name` / `enabled`。
开箱模板见本仓 `examples/octop-mcp-connector.example.json`。

1. **签发令牌**：每用户一对（`mcp` 给 Octop、`bridge` 给桥 EXE），
   静态环境变量或 `POST /admin/tokens/issue`。
2. **Octop 连接器配置**：Transport `streamable_http`；
   URL `https://<server>/mcp/localoctop/`；Bearer 填该用户的 **mcp_token**。
3. **探测**：Octop 发 `initialize` → `tools/list`（官方 mcp SDK 客户端，
   带 `Accept: application/json, text/event-stream`）。适配器返回
   `serverInfo.name=localoctop` 与 4 个只读工具。
4. **调用链**：Octop `tools/call` → 适配器按 mcp_token 找到该用户的桥会话 →
   经 WSS 下发到员工本机 → 桥在白名单内执行并回传 → 适配器渲染成 MCP content。
5. **员工侧**：运行本仓 `localoctop.exe`，用 **bridge_token** 拨入
   `wss://<server>/mcp/localoctop/ws`。未连接时 tools/call 返回错误码 `4101`。

### 安全边界（服务端层）

- **双令牌分向**：mcp_token 与 bridge_token 是两个独立命名空间，互不通用，
  可独立吊销；只存 SHA-256 哈希，明文只出示一次。
- **每用户隔离**：会话注册表以认证得到的 `user_id` 为键，调用方无法指定
  他人会话（`test_user_isolation_bob_cannot_reach_alice_bridge`）。
- **MCP 会话与凭据绑定**：SDK 会话只能由创建它的令牌使用（跨令牌 404）。
- **重连替换**：同一用户重连时旧会话被原子替换并关闭，其未完成调用全部失败。
- **写双重开关**：服务端 `allow_write`/白名单 **且** 客户端注册时声明
  `write_enabled`，两者都满足才放行写工具；默认全关。
- **管理员停用**：`LOCALOCTOP_DISABLED=true` 时所有 tools/call 立即拒绝（4001）。
- **结果大小护栏**：客户端被攻陷时，服务端对回传 content 有 24MB 上限。
- **审计落盘**：每次调用（allow/deny/error）写入 `LOCALOCTOP_AUDIT_LOG`
  （JSON lines，带轮转）。

## 上游兼容性核对（每次 Octop 升级后）

1. `custom_mcp.py` 的 transport 枚举/探测行为是否变化（本适配器按
   streamable_http + 官方 mcp SDK 服务：POST 返回 JSON 或 SSE，
   `Mcp-Session-Id` 头校验，GET 流 / DELETE 终止均为 SDK 标准行为）。
2. 连接器 UI 是否仍支持「自定义 MCP + Bearer（headers）」录入。
