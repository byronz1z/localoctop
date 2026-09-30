# Changelog

本文件记录 localoctop 单仓所有组件的显著变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号遵循语义化（bug 修 +0.0.1，功能 +0.1.0）。

## [Unreleased]

### 计划中（0.6.0）
- 桥核心：WebSocket 心跳保活（ping/pong，防 NAT 空闲回收）+ 断线指数退避快速重连
- 桌面端：真实连接状态显示（以心跳存活为准，掉线立即变"重连中"）；手动连接/断开按钮；NSIS 安装器（installMode currentUser）；开机自启勾选
- 服务器：令牌热加载（改 .env 后免重启生效）
- 治理：单仓架构（bridge/ + server/ + desktop/ + ops/）、治理文件（CHANGELOG/RELEASE_CHECKLIST/AGENTS/VERSIONS）、任务书归档 docs/tasks/

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
