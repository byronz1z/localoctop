# T3' — 桌面：第六状态「已在别处登录」+ 产品命名整改

## 需求来源

- **[用户裁定]** 界面要展示被接管的原因：新增独立状态「已在别处登录」——告知用户"该令牌已在另一台设备接管，本机已暂停，点「连接」可收回"，替代无因的"重连中/连接不稳定"。
- **[用户裁定]** 产品命名按项目名：**localoctop**。AI 自起的「Octop 本地文件桥」作废（[AI 判定·已被用户纠正]：该命名未经报备）。
- **[用户裁定]** 默认安装路径：**`D:\Program Files\localoctop\`**（Program Files 下新建子文件夹）。
- **[事实]** 命名污染分布（grep 实测 7 文件）：desktop/main.go:62、desktop/wails.json:16,18、desktop/internal/tray/tray.go:76,136、desktop/frontend/index.html:6、desktop/frontend/src/main.js:504、desktop/frontend/dist/index.html（构建产物，随构建更新）。

## 目标

桌面端正确呈现"已在别处登录"第六状态；全部用户可见命名统一为 localoctop；安装器默认装 `D:\Program Files\localoctop\`。

## 范围（只改 desktop/）

1. **第六状态**：main.js `renderStatus` 新增 `parked` 分支——状态卡：标题「已在别处登录」，副文案"该令牌已在另一台设备上接管，本机已暂停。点「连接」可从那台设备收回"；tray.go `TrayLabel` 同步该状态；五态 classify 扩为六态；`updateConnButtons`：parked 时「连接」按钮可用（点击即收回接管）。
2. **命名整改**（逐处替换为 localoctop）：
   - `wails.json`：productName=localoctop、comments 改写（注意：productName 变更会使构建输出变为 `localoctop.exe`，NSIS 脚本与 CI 产物文件名同步核对）；
   - `main.go` 窗口 Title、`tray.go` 托盘 tooltip、`index.html` title、`main.js` 欢迎语；
   - 安装器 `project.nsi`（wails 自动生成模板如不支持注入，改用自定义 nsis 模板）：默认安装目录 `D:\Program Files\localoctop`、installMode perMachine（弹 UAC）、卸载兼容：不自动清理旧用户目录（用户自行卸载旧版），安装页文案用 localoctop。
3. UI 文案遵循 docs/UI-COPY.md 既有术语表。

## 禁止事项

- 不改桥核心（T2' 负责）、不改 server/；
- 不做主题、语言切换等范围外美化；
- 不碰 VERSION 文件（中枢统一升版）。

## 验收标准

- 根模块与 desktop 模块 `go build/vet/test` 全绿；
- grep 确认仓内用户可见文案不再出现「Octop 本地文件桥」；
- 报告附：六态文案清单、nsis 关键 diff、构建产物 exe 名确认。

## 环境

Go/Wails 环境变量见 AGENTS.md；NSIS 在 D:/tmp/nsis/Bin；可本地 wails build 验证，禁止连生产。
