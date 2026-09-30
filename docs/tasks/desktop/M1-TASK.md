# M1 任务书：localoctop-desktop 原生窗口壳（GLM 5.3 执行）

## 背景与定位

`D:\SelfHosted\localoctop`（主仓）现有产品 EXE = Go 桥 + 内嵌网页控制台（127.0.0.1:19880 浏览器打开）+ 托盘，控制台子进程编译导致黑框，且引导页靠网页。用户裁定：仿 WebCodex Desktop 做真原生客户端。

本工程 `D:\SelfHosted\localoctop-desktop`（已 wails init，基线构建已通过）是**新桌面 EXE 的家**。桥核心包 `github.com/byronz1z/localoctop` 与其 `internal/appcfg`（配置持久化）**原样复用、一行不改**——它们是 v0.5.0 已验证资产。

⚠️ `internal/` 包外部不可 import。**允许的做法**：go.mod 加 replace 指向本地主仓路径后 import 主仓的 `internal/appcfg`（同机 replace 场景 Go 允许跨 module 引用 internal，前提 module 路径前缀一致——若编译报 internal 不可引用，则把 appcfg 的 File/Load/Save/Default/EnabledDirs/RememberServer/Path/AuditPath **复制**进本工程 `internal/appcfg`，注释标明"copy of localoctop/internal/appcfg@v0.5.0"）。先试 replace，不行就复制，不要重构主仓。

## M1 交付范围（只做这些，M2/M3 的不做）

1. **main.go 重写**：Wails 窗口（标题"Octop 本地文件桥"，1024x768，可调整），Bind 一个 App 结构。
2. **App 绑定方法**（前端经 window.go.xxx 调用）：
   - `LoadConfig() appcfg.File` — 读配置（含 existed 标志区分首启）
   - `SaveConfig(next) error` — 校验+落盘+热重建桥（逻辑对照主仓 cmd/bridge/main.go 的 apply 闭包 + rebuildLocked，照抄语义：桥失败返回错误、旧桥继续跑）
   - `GetStatus() Status` — 结构化连接状态（connected/clientID/serverURL/lastError）
   - `PickDirectory() string` — **用 Wails runtime.OpenDirectoryDialog**（原生对话框，彻底移除 PowerShell 脚本路径——这就是 GBK 乱码 bug 的根治，不再需要任何 .ps1）
   - `GetCommonDirs() []CommonDir` — 常用位置（对照主仓 console.commonDirs 逻辑：用户目录/桌面/文档/下载/OneDrive 变体，存在性过滤）
   - `GetVersion() string` — 桥核心 localoctop.Version
3. **桥生命周期**：startup 时 Load→构建 Bridge→`go b.Run(ctx)`；OnStatus/OnAudit 钩子状态存 App 结构（原子锁），前端轮询或 Wails EventsEmit 推送（选 EventsEmit："bridge:status"、"bridge:audit"）。
4. **前端 M1 最小可用**：frontend/dist 用纯 HTML/JS（vanilla 模板现状）实现引导页表单：服务器地址、令牌（password 框）、目录列表（原生选择器添加/启用开关/删除）、写入开关（**默认勾选**，标签注明"关闭=只读"）、保存按钮；连接状态横幅（绿=已连接 clientID，红=错误信息）。中文文案。样式简洁即可，M2 再打磨。
5. **托盘**：M1 先不做（M3 一起），但 main.go 里**关闭窗口=退出进程**的默认行为先保留。
6. **构建产物**：`wails build` 产出 `build/bin/localoctop-desktop.exe`（windowsgui，无黑框）。

## 明确不做（防止跑偏）

- 不改主仓 localoctop 任何文件（只读依赖；除上述 replace 方案的 go.mod）
- 不做 M2 的界面美化/最近服务器下拉、不做 M3 的托盘/自启/活动页、不碰 headless 模式
- 不引前端框架/构建器（保持 vanilla），不引 cgo
- 不改版本号、不打 tag、不动 CI（那是 M4）

## 验收标准（GLM 5.3 自测后我复验）

1. `go build ./... && go vet ./...` 零错误（环境变量：GOROOT=D:\tmp\go GOPATH=D:\tmp\gopath GOTMPDIR=D:\tmp\gotmp PATH 前插 D:\tmp\go\bin;D:\tmp\gopath\bin；C 盘只剩 0.1GB，**任何写盘动作不得落 C 盘**）
2. `wails build` 出 EXE；PE subsystem=GUI（我已用 python 脚本核验方法，会复验）
3. 双击 EXE：原生窗口出现、无黑框、无浏览器弹窗；表单填入真实参数保存后桥连上服务器（服务器地址 wss://zbsworkoctoplink.api.zhangbaoshan.cn:8445/mcp/localoctop/ws，token 用 employee-setup.md 里那串）；状态横幅变绿显示 client_id
4. 点"选择目录"弹**原生**对话框（非 PowerShell），选目录后列表出现；取消返回无异常
5. 配置文件位置与主仓 appcfg.Path() 一致（同一份配置，桌面版和旧 EXE 可互换）
6. 停掉 EXE 后服务器 admin/sessions 里该桥下线

## 工作方式

- 逐项做完打勾汇报，遇阻（编译错/不确定的 Wails API）停下报错原文，不猜测不绕
- 完成后输出：改动文件清单 + `wails build` 尾部输出 + 自测各验收项结果
