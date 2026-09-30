# Changelog

本文件记录 localoctop 单仓所有组件的显著变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号遵循语义化（bug 修 +0.0.1，功能 +0.1.0）。

## [0.6.0] - 2026-09-30

首次单仓统一发版（bridge + server + desktop 同号）。起因：员工实机验收暴露 70 秒必掉线、状态假显示、无安装逻辑三类问题，一次性修完。

### 桥核心（bridge/）
- **心跳 pingLoop**：每 PingInterval（25s）主动发 ping，空闲会话不再被读超时/NAT 回收掐断（修复"70 秒必掉线"）；ping 写失败立即断开走快速重连，不再干等超时
- **StatusDetail 状态透出**：Connected / ReconnectAttempt / LastError / LastPingAt / LastPongAt；新增 `OnStatusDetail` 回调与 `Bridge.StatusDetail()` 快照（旧 `OnStatus` 不动，零破坏）

### 服务器适配器（server/）
- **令牌热加载**：后台每 30s 轮询 env 令牌变量（`LOCALOCTOP_TOKEN_POLL_INTERVAL` 可调，0=禁用），变更原子换表生效；新增 `POST /admin/tokens/reload` 立即重载（admin 鉴权，diff 只含 user_id 无明文）；换表为复制-改-原子替换，并发鉴权无中间态；env 之外动态签发的令牌不受轮询影响

### 桌面客户端（desktop/）
- **五态真实状态卡**：已连接（含最后心跳，>90s 无 pong 黄牌"心跳异常"）/ 连接中 / 重连中（第 N 次，附原因）/ 已断开 / 未配置——掉线立即如实显示，不再假"已连接"
- **手动连接/断开**：断开为用户意志（`userDisconnected`），自动重连不得覆盖；启动仍自动连接
- **修复预存死锁**：SaveConfig 持锁时 emitStatus 重入加锁导致同步保存必卡死——重构为锁外 emit（"运行卡死"根因之一）
- **NSIS 安装器**（`wails build -nsis`）：currentUser 免管理员、开始菜单+桌面快捷方式、卸载保留 %AppData% 用户数据、完成页可选立即运行
- **开机自启**：设置页勾选，HKCU Run 注册表 + `--minimized` 启动参数（直接进托盘）
- 托盘 tooltip 同步五态真实状态

### 治理
- 单仓架构（阶段 0）：adapter/→server/、desktop/ 迁入、ops/ 收编、appcfg 副本消除（ConfirmExit/Autostart 直引主仓包）
- 治理文件：CHANGELOG / RELEASE_CHECKLIST / AGENTS / VERSIONS；桌面 CI 流水线 release-desktop.yml（tag desktop-v* → EXE+安装器+PE 断言门禁）

## [Unreleased]

## [0.5.0] - 2026-09-29

三组件版本对齐：桥核心 v0.5.0 / 适配器 1.3.0 / desktop-v0.1.0（此版本号制度自 0.6.0 起统一）。

### 桥核心（v0.5.0）
- 官方 MCP filesystem 全 17 工具（读 10 / 写 7）
- 回收站删除（Windows 复原语义）、edit dryRun、zip 打包解包往返、正则搜索
- junction/符号链接路径归一化修复
- 令牌双命名空间（lfs-mcp- / lfs-bridge-），按 kind 隔离鉴权

### 服务器适配器（1.3.0）
- 适配 17 工具全量转发
- 会话注册表（admin/sessions 只读视图）
- e2e 16 项全绿（含回收站验证）

### 桌面客户端（desktop-v0.1.0）
- Wails v2.10.1 原生窗口 + fyne.io/systray 托盘（关窗进托盘、桥不断）
- 三页 UI（连接/活动/设置）、活动审计流 500 条环形缓冲、仅错误过滤
- 目录选择器原生对话框（根治 GBK 乱码）
- confirm_exit 退出确认（默认开，老配置兼容）

### 已知问题（0.6.0 修复目标）
- 桥无心跳：NAT 空闲约 70 秒回收连接，客户端状态显示失真（假"已连接"），AI 调工具间歇性 stream_error
- 改 .env 需重启容器（无热加载）
- 无安装器：裸 EXE 解压即跑
- 长时间运行 UI 卡死报告（与重连风暴相关，随心跳修复验证）
