# localoctop-desktop

localoctop **桌面客户端**（Windows GUI）。基于 [Wails v2](https://wails.io)（Go + 系统 WebView2，无 Electron），复用主仓 [localoctop](https://github.com/byronz1z/localoctop) 的 Bridge 核心作为 Go 模块依赖（`github.com/byronz1z/localoctop`，当前 v0.5.0），为员工提供图形化的桥配置、启停与托盘驻留体验。

## 项目定位

- **它做什么**：在 Windows 上以原生窗口运行本地文件桥 —— 配置服务器地址/Token/白名单目录、热重建桥连接、常驻系统托盘、展示连接状态与审计事件。关闭窗口默认最小化到托盘，桥不断连。
- **它不做什么**：不实现桥协议本身。所有连接/路径白名单/审计/重连逻辑都来自主仓 `localoctop` 模块，本仓只做桌面壳（窗口、托盘、设置界面、配置持久化）。

## 与主仓的关系

- **依赖方向**：本仓 `go.mod` 直接 `require github.com/byronz1z/localoctop v0.5.0`（远端可解析，无 replace 指令），桥核心升级 = 升级该依赖版本。
- **appcfg 副本同步义务**：`internal/appcfg/` 是主仓 `localoctop/internal/appcfg` 的近逐字副本（Go internal 规则禁止跨模块导入，副本是任务书认可的方案）。两个程序**共享同一个配置文件**（路径、JSON 形状、默认值完全一致），因此：
  - 主仓 appcfg 的任何字段/默认值/JSON 键变更，**必须**同步到本副本，否则两边读写会分叉；
  - 本副本的增量字段（如桌面端独有的 `confirm_exit`）必须保持**可加性**：`omitempty`、主仓不识别也能容忍（encoding/json 忽略未知键），不得改变既有键的语义。
- **发版协同**：主仓 tag `v*` 发 headless 包；本仓 tag `desktop-v*` 发桌面包（见下文 CI）。

## 双包选择指南

| 包 | 仓 / tag | 适用对象 | 形态 |
|---|---|---|---|
| `localoctop-windows-amd64.zip`（headless） | 主仓 localoctop，tag `v*` | 服务器 / 无人值守 / 脚本部署 | 控制台 EXE，命令行参数或共享配置文件驱动 |
| `localoctop-desktop-windows-amd64.zip`（桌面） | 本仓，tag `desktop-v*` | 员工日常使用 | GUI 窗口 + 托盘，图形化配置，窗口关闭不断连 |

简单说：**人用选 desktop，机器用选 headless**。两者读同一份配置文件（`%AppData%\localoctop\config.json`），在同一台机器上可互换使用。

## 配置文件位置

- Windows：`%AppData%\localoctop\config.json`
- Linux：`$XDG_CONFIG_HOME/localoctop/config.json`
- macOS：`$HOME/Library/Application Support/localoctop/config.json`

同目录下的 `audit.jsonl` 为默认审计日志。配置文件权限 0600（含桥 Token）。

## 构建方法

环境要求：Go 1.23+、Node 20+、[Wails CLI v2.10.1](https://wails.io/docs/gettingstarted/installation)、Windows（WebView2 运行时）。

```powershell
# 安装 Wails CLI（一次）
go install github.com/wailsapp/wails/v2/cmd/wails@v2.10.1

# 构建（发布形态，无控制台窗口）
wails build -ldflags "-s -w"

# 产物
build/bin/localoctop-desktop.exe
```

本地开发：`wails dev`（前端热重载；浏览器调试入口 http://localhost:34115）。

## CI 发版（GitHub Actions）

发版流程见 `.github/workflows/release.yml`：推送 `desktop-v*` tag 触发 —— windows-latest 上 Go 1.23 + Node 20，`wails build -ldflags "-s -w"` 构建，随后用 Python 断言 PE 头 `subsystem == 2`（windowsgui，无控制台窗口），再打包 EXE + README 为 `localoctop-desktop-windows-amd64.zip` 并附 sha256，通过 softprops/action-gh-release 上传到该 tag 的 GitHub Release（自动生成 release notes）。

```powershell
git tag desktop-v0.1.0
git push origin desktop-v0.1.0   # CI 完成发版
```

**CI 构建是唯一部署真源**（用户裁定）：本地 `wails build` 产物仅用于自测，不作为发布物。

## 测试

```powershell
go build ./... && go vet ./... && go test ./...
```

- `internal/appcfg`：roundtrip / legacy 兼容（老 JSON 缺 `confirm_exit` → 默认开启）/ `allow_write` 默认值三组测试（与主仓同款）。
- 根包：M3 settings 绑定测试（对真实配置文件快照-恢复后执行）。

## 目录结构

```
main.go              Wails 入口（窗口/托盘/生命周期接线）
app.go               绑定到前端的 App：配置、桥热重建、状态
internal/appcfg/     配置持久化（主仓 appcfg 副本，见同步义务）
internal/tray/       系统托盘（fyne.io/systray）
frontend/            前端（Wails vanilla 模板演进）
build/               Wails 构建资产（icon、NSIS）
.github/workflows/   release.yml（desktop-v* tag 发版）
```
