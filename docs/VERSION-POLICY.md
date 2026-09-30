# VERSION-POLICY — 版本与发布管理规范

> 来源：**[用户裁定 2026-09-30]**「Docker 和 Desktop 应该是不同的建立点，但不应该打不同的包，它们应该是在同一个版本，不可能存在不同版本号的问题。」

## 一、核心原则

1. **全仓只有一个版本号。** 服务器（Docker）与桌面（Desktop）是两条**构建管线**，不是两个版本。任何时刻三组件（bridge / server / desktop）对外版本号必须完全一致。
2. **禁止版本号分裂。** 历史事故（桌面壳 desktop-v0.1.0 内嵌桥 0.5.0，导致用户实测误判"版本一直不对"）不允许再发生。
3. **单 tag → 单 Release → 多产物。** 一个 tag 触发两条管线，产物合并进同一个 GitHub Release，用户只有一个下载入口。

## 二、版本真源（Single Source of Truth）

仓库根目录 `VERSION` 文件是唯一版本真源，内容仅一行（如 `0.6.1`）。

各组件在构建时**读取或断言**与 `VERSION` 一致，禁止各自硬编码漂移：

| 组件 | 版本位置 | 一致性要求 |
|---|---|---|
| bridge（Go） | `protocol.go` 的 `Version` 常量 | == VERSION |
| server（Python） | `localoctop/config.py`（或 `__version__`） | == VERSION |
| desktop（Wails） | `desktop/frontend/wails.json` + 关于页 | == VERSION |

## 三、发布流程（0.6.1 起生效）

1. 中枢改 `VERSION` 文件 + 同步三组件版本常量，一个 commit 完成；
2. 打**一个** tag：`v0.6.1`（**废除** `desktop-v*` 双 tag 制度）；
3. push tag 后两条管线并行构建：
   - `release.yml` → 服务器源码包/镜像产物
   - `release-desktop.yml` → 桌面 setup.exe + 便携 zip
4. 两条管线产物**上传到同一个 Release**（`v0.6.1`），附 checksums；
5. CI 一致性断言（两管线第一步）：`VERSION == protocol.go == server == wails.json`，不一致立即失败，禁止出包。

## 四、Release 生命周期纪律

- **下载页只保留当前可用的最新版本**，旧版本 release 发布新正式版后即删除（tag 保留作历史回溯）——**[用户裁定 2026-09-30]**「把之前的错误版本删掉，避免误判」；
- 坏 release（产物错误/流水线失败残留）：先删 release，修好后重打 tag，按已报备的 tag 移动政策执行；
- 已归档仓库（如 localoctop-desktop）不得残留任何可下载产物，防止搜索误下。

## 五、生产部署对应

生产容器镜像 tag 必须与 `VERSION` 一致（`localoctop-server:0.6.1`），部署前断言镜像版本号 = VERSION；生产部署始终走 ENV-POLICY 报备流程。
