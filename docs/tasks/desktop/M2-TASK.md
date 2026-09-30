# M2 任务书：引导页打磨（GLM 5.3 执行）

工作目录 D:/SelfHosted/localoctop-desktop（M1 已完成并通过复验：app.go/main.go/frontend 已就绪，桥真实连接已验证）。环境变量：GOROOT=D:\tmp\go、GOPATH=D:\tmp\gopath、GOTMPDIR=D:\tmp\gotmp、PATH 前插 D:\tmp\go\bin;D:\tmp\gopath\bin。C 盘禁止写盘。禁止 git commit。

## 目标

把 M1 的"功能可用"引导页打磨到 WebCodex Desktop 观感水准的中文原生界面。参考形态（yyjeqhc/webcodex Desktop）：侧边栏 Home/连接/活动/设置、状态卡片、大按钮引导流、简洁浅色。**不做侧边栏**（M1 表单是单页，M2 只做单页内的观感升级；侧边栏留给 M3 多页时一起做）。

## 交付范围（只做这些）

1. **最近服务器下拉**：serverURL 输入框下方加"最近使用"下拉（数据来自 LoadConfig 返回 file.recent_servers，选中之同时回填该条目的 token）。appcfg.RememberServer 已在 SaveConfig 里调用，无需改 Go 侧（如需暴露清空历史可加小按钮，非必须）。
2. **表单校验与提示**：保存前前端校验——serverURL 必须 wss:// 或 ws:// 开头、token 非空、至少一个启用目录；不满足时红字提示具体缺什么，不调 SaveConfig。
3. **令牌框体验**：password 型 + "显示"眼睛切换按钮；占位提示"由管理员发放的桥令牌"。
4. **状态卡片区**：banner 升级为状态卡片：连接状态大字（已连接/未连接/连接中）、client_id、服务器地址、重连次数（status.reconnects 已有）、最后审计时间（status.last_audit_ts）。未连接且 last_error 为空时显示"等待保存配置"。
5. **常用位置 chips 美化**：现有功能保留，chip 加图标感（用 CSS/字符即可，不引图标库）。
6. **视觉规范**：浅色主题、系统字体栈（"Segoe UI", "Microsoft YaHei UI"）、主色用一个稳重的蓝绿系（自定具体值，全局一致）、卡片圆角+轻阴影、间距统一 8px 栅格。整体接近现代桌面应用，不要"网页感"。
7. **版本角标**：右下角 v0.5.0 小字（GetVersion 已有）。

## 明确不做

- 不加侧边栏/多页面/活动页（M3）
- 不改 Go 侧任何绑定方法签名（app.go 只允许加注释级微调；若确需新字段走 EventsEmit 现有事件）
- 不引任何 npm 依赖/图标库/字体文件（纯 CSS）
- 不动 wails.json/main.go 构建配置

## 验收标准（自测后我复验）

1. `wails build` 成功；EXE 启动后：空配置显示欢迎引导文案；填无效值点保存出现具体红字提示而非请求发出
2. 预填真实配置（config.json 已有真实值）启动：表单回填、最近服务器下拉含历史条目、状态卡片显示已连接+client_id
3. 令牌眼睛切换生效；chips 一键添加正常
4. 视觉整体协调（我实机截图复验）
5. go build/vet 双零错误
