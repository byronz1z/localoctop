# Octop 接入三步（开箱即用）

本目录随发布包提供：把云端 AI 助手（经 Octop「自定义 MCP 连接器」）接到你本机
白名单目录，一共三步。两个占位符 `CHANGE_ME_*` 必须替换。

> **两个令牌，别混用**（适配器是双令牌模型）：
> - **bridge_token**：填在桥 EXE 侧（本程序拨入云端用）；
> - **mcp_token**：填在 Octop 连接器侧（下面第 3 步）。
> 二者由适配器管理员分别签发（`POST /admin/tokens/issue`，`kind` 分别为
> `bridge` / `mcp`），互不通用。

## 第 1 步：跑起云端适配器容器

在服务器上（本仓 `adapter/` 目录，克隆仓库或下载 Release 资产
`octop-local-bridge-server-*.zip` 即得）：

```bash
docker build -t octop-local-bridge-server:1.0.0 .
docker run -d --name octop-local-bridge-server -p 127.0.0.1:8080:8080 \
  -e LOCALFS_TOKENS="你的用户名:CHANGE_ME_MCP_TOKEN" \
  -e LOCALFS_BRIDGE_TOKENS="你的用户名:CHANGE_ME_BRIDGE_TOKEN" \
  -e LOCALFS_AUDIT_LOG=/app/logs/calls.jsonl \
  -v octop-local-bridge-audit:/app/logs \
  octop-local-bridge-server:1.0.0
```

放在反向代理（Caddy/Nginx/Traefik）之后，对外提供
`https://你的域名/mcp/localfs/` 与 `wss://你的域名/mcp-localfs/ws`。
详细配置见 `adapter/README.md` 与 `adapter/docker-compose.example.yml`。

## 第 2 步：桥 EXE 引导页填地址

双击运行本包中的 `octop-local-bridge.exe`（或已安装版本的桌面模式），浏览器自动打开
控制台引导页：

1. 选择白名单目录（AI 只能看到这些目录）；
2. 服务器地址填 `wss://你的域名/mcp-localfs/ws`；
3. 令牌填 **bridge_token**（`CHANGE_ME_BRIDGE_TOKEN` 换成的那个）；
4. 「保存并连接」，状态页显示「已连接」即成功。

无人值守环境可用 headless 模式：

```powershell
.\octop-local-bridge.exe --headless -server "wss://你的域名/mcp-localfs/ws" `
  -token CHANGE_ME_BRIDGE_TOKEN -dir "D:\我的资料"
```

## 第 3 步：Octop 后台贴连接器配置

把 `octop-mcp-connector.example.json` 的内容贴入 Octop 后台
「连接器 → 自定义 MCP」保存（对应接口 `PUT /api/connectors/custom-mcp`）：

- `url`：`https://你的域名/mcp/localfs/`（公网必须 https；回环/内网可 http）；
- `headers.Authorization`：`Bearer ` + **mcp_token**（不是第 2 步那个！）；
- 保存后点「测试」：Octop 会做 `initialize` + `tools/list` 探测，
  成功时列出 4 个只读工具（`list_directory` / `read_file` /
  `search_files` / `get_file_info`）。

字段依据：Octop `src/octop/infra/connectors/custom_mcp.py` 的
`normalize_server_spec`（transport/url/headers/display_name/enabled）与
`harness_spec_for_server`（Bearer 经 `headers.Authorization` 注入）。

## 排错速查

| 现象 | 原因 |
|---|---|
| Octop 探测 401 | mcp_token 错，或误填了 bridge_token |
| 桥连不上（反复重连） | 地址/端口/反代未转发 WebSocket，或 bridge_token 错 |
| 工具调用报 `4101` | 你的桥没在线——启动/检查第 2 步 |
| 工具调用报 `4001` | 请求的路径不在白名单内（安全拒绝，正常） |
