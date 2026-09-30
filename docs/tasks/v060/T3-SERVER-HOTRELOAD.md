# T3 服务器令牌热加载（0.6.0）

仓库：D:/SelfHosted/localoctop，分支 feature/v0.6.0（已建好）
范围：**只改 server/ 目录**（Python 适配器），禁止碰其他任何目录。
先读 AGENTS.md 与 server/ 现有结构（localoctop/ 包、tests/）再动手。

## 背景

当前令牌在容器启动时从环境变量读一次，加员工必须 `docker compose up -d` 重建容器（restart 都不生效——env 不重读），桥全体闪断。需要热加载。

## 任务

### 1. 周期轮询重载

- 后台 asyncio task（随 app 启动），每 30 秒读一次 `LOCALOCTOP_MCP_TOKENS` / `LOCALOCTOP_CLIENT_TOKENS` 环境变量
- 与上次内容比对（hash 或直接字符串比较），**无变化零动作**；有变化则原子更新内存令牌表并 `log.info` 记录变更（新增/移除了哪些 user_id，**不得打印令牌原文**）
- 注意：生产环境变量改了必须重建容器才可见（docker 语义），所以轮询主要覆盖 k8s/直接进程部署场景；真正解决"改 .env 不重启"的是第 2 项 admin API。两者都做。

### 2. admin 重载 API

- `POST /admin/tokens/reload`，鉴权用现有 admin token（与 /admin/sessions 同机制）
- 立即重读环境变量并更新令牌表，返回 JSON：`{"reloaded": true, "mcp_users": [...], "bridge_users": [...]}`
- 使用方式（写进 docstring）：运维改宿主机 .env 后 `docker compose up -d` 一次性重建仍是最可靠路径；本 API 用于同容器内 env 已更新的场景与自动化调用

### 3. 令牌表更新线程安全

- 复核现有 authenticate 路径读令牌表的方式，更新必须是原子替换（整体换 dict 引用或加锁），禁止"边遍历边改"

### 4. 测试（server/tests/）

- 轮询重载：env 注入新令牌 → 触发 reload → 新令牌认证通过、旧令牌按语义处理（移除即拒绝）
- reload API：admin 鉴权通过/失败两路径；响应含用户列表且**不含令牌值**
- 并发：reload 与 authenticate 并发无竞态（至少跑现有全量测试不回归）

## 质量门（必须全过并附原文）

```
cd server && python -m pytest tests/ -q
```

## 交付

git add server/ 下改动 + commit（中文信息，前缀 `feat(server):`），禁止 push。
报告：改动文件清单、reload 逻辑时序、测试输出原文、git log -1。
