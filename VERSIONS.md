# VERSIONS — 组件版本对照表

> 单仓单版本号制度：对外唯一版本号，三组件同发。本表由中枢在发版时更新。

| 对外版本 | bridge/（Go） | server/（Python） | desktop/（Wails） | 发布日期 | tag | 备注 |
|---|---|---|---|---|---|---|
| 0.5.0 | v0.5.0 | 1.3.0 | desktop-v0.1.0 | 2026-09-29 | v0.5.0 / desktop-v0.1.0 | 单仓制前最后一批分开发布；17 工具+托盘+审计 |
| 0.6.0 | 待发 | 待发 | 待发 | 未定 | v0.6.0 / desktop-v0.6.0 | 心跳保活+真实状态+安装器+热加载（首次单仓统一发版） |

## 历史版本号映射（合仓前）

| 旧仓 | 旧版本 | 内容 |
|---|---|---|
| byronz1z/localoctop（现 bridge/+server/） | v0.1.0 ~ v0.5.0 | 桥核心迭代：MCP 工具面扩至 17、令牌双命名空间、回收站、zip 往返 |
| byronz1z/localoctop-desktop（已 archived） | desktop-v0.1.0 | 桌面壳：Wails 窗口/托盘/三页 UI/审计流 |
| ZBSwork-OCTOP-service（保留，私有） | 随发 | 生产部署描述/.env/证书（敏感件永不进公开仓） |
