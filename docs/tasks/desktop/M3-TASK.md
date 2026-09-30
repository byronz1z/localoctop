# M3 任务书：托盘常驻 + 多页面（GLM 5.3 执行）

工作目录 D:/SelfHosted/localoctop-desktop（M1/M2 已完成）。环境变量同前（GOROOT=D:\tmp\go、GOPATH=D:\tmp\gopath、GOTMPDIR=D:\tmp\gotmp，C 盘禁止写盘）。禁止 git commit。

## 目标

仿 WebCodex Desktop 运行态：关窗口进托盘常驻、托盘状态菜单、侧边栏多页面（连接/活动/设置）。

## 交付范围

1. **托盘常驻**：集成 fyne.io/systray（主仓同款，纯 syscall 无 cgo）与 Wails 共存。注意点：systray.Run 在 Windows 需主 goroutine 消息循环——采用 systray 的 Register/RunExternal 模式或独立 goroutine + systray.RunWithExternalLoop（查证 systray 文档选正确 API；Wails 主线程归 Wails）。托盘项：状态行（已连接 client_id / 未连接）、"打开窗口"（runtime.WindowShow）、分隔线、"退出"（cancel 桥 + runtime.Quit）。
2. **关窗即隐藏**：Wails OnBeforeClose 拦截——最小化到托盘而非退出；托盘"退出"才真正结束进程。首关可弹一次确认框（"退出后桥将断开，确定？"带"不再提示"），存 appcfg 新字段 confirm_exit（默认 true=弹确认）。
3. **侧边栏布局**：左侧固定栏（宽 ~200px）三页导航：连接（=M2 表单页）、活动、设置。当前页高亮。
4. **活动页**：审计事件流——EventsOn("bridge:audit") 收集进内存数组（上限 500 条，新的在上），每条显示时间/方法/路径/结果（AuditEvent 字段查 localoctop 包 protocol 定义），提供"仅错误"过滤 checkbox。空态文案"暂无操作记录"。
5. **设置页**：open_browser 字段在桌面版已无意义→改造成"关闭窗口时最小化到托盘"说明 + confirm_exit 开关 + 审计日志路径只读展示 + 版本/配置路径展示（appcfg.Path）。
6. **连接页**：M2 表单原样搬入，布局适配侧边栏。

## 明确不做

- 不做开机自启（留 M4 评估）、不做日志文件导出、不改桥核心/appcfg 语义（confirm_exit 新字段允许加）
- 不引 UI 框架；托盘图标沿用主仓 icon.ico（copy 进本工程 assets，embed）

## 验收标准

1. build/vet 零错误；wails build 成功
2. 实机：关窗→托盘在、桥不断（admin/sessions 仍在线）；托盘"打开窗口"恢复；托盘"退出"→进程消失且服务器端下线
3. 活动页显示真实审计事件（对 wtest 目录做一次 MCP 调用后出现记录）；设置页字段正确
4. 真实配置连接行为与 M1 复验结果一致（不回归）
