# M4 任务书：质量关 + GitHub 正式发版（GLM 5.3 执行 + 中枢复验）

前置：M3 完成并通过中枢复验。工作目录 D:/SelfHosted/localoctop-desktop。环境变量同前。**本任务书允许 git 操作（按指令执行），禁止 push 到 GitHub（push 由中枢做）。**

## 背景

桌面客户端（localoctop-desktop）将与主仓 localoctop（桥核心+服务器适配器）以**两个仓协同**的方式发版。打包规范按用户裁定：走 GitHub 正常流程（CI 构建=唯一部署真源），禁止本地 build 当真源。

## 交付范围

### 1. 工程收尾
- 工程转正为 git 仓：`git init` + 合理 .gitignore（排除 frontend/node_modules、build/bin、wails.json 的本地调试痕迹）+ 首次 commit（信息按惯例中文，说明来源：localoctop 桌面客户端 v0.5.0 核心）
- README.md 重写：项目定位（Octop 本地文件桥桌面客户端）、构建方法（wails build + 环境变量要求）、配置文件位置、与主仓关系（appcfg 副本来源与同步义务）
- **replace 移除评估**：go.mod 的 `replace github.com/byronz1z/localoctop => D:\SelfHosted\localoctop` 在 CI 上必挂（路径不存在）。方案：改成 `replace github.com/byronz1z/localoctop => ../localoctop` 也不行（CI 无兄弟目录）→ **正确做法：把 desktop 工程挪进主仓成为 `desktop/` 目录**（主仓已有未跟踪 desktop/ 目录，经查是 M1 前的遗留空壳，先删），go.mod 用主仓本地模块路径；或 desktop 独立仓 + go.mod require 固定版本（推主仓 v0.5.0 tag 可解析）。**二选一并说明理由**，选独立仓则 go.mod 需 github.com/byronz1z/localoctop v0.5.0（已发布，可解析）。
- 单元测试：至少给 appcfg 副本补主仓同款测试（roundtrip/permissions/legacy），给 app.go 的 rebuild/validate 语义写可测函数的测试（不要求 UI 自动化）

### 2. CI 发布流（GitHub Actions）
- `.github/workflows/release.yml`（desktop 仓或主仓，按上面选型定）：push tag `desktop-v*` 触发
  - runner: windows-latest，装 Go 1.23 + Node 20
  - build: `wails build -ldflags "-s -w"`（非 windowsgui 检查：构建后用 python 断言 PE subsystem=2，失败即 fail）
  - 产物：zip（EXE+README）+ sha256 文件，softprops/action-gh-release 上传
  - 主仓 CI 不受影响（若挪进主仓，加 path 过滤避免无关触发）
- headless 包：主仓现有 release 流不变（双包并存：主仓 windows-amd64.zip = headless/服务器用；desktop zip = 员工 GUI 用），README 里写清两者区别与选择建议

### 3. 质量清单执行
- `go build ./... && go vet ./... && go test ./...` 全绿
- `wails build` 成功 + PE subsystem=2 断言通过
- 中枢将实机执行人工点验清单（我在你完成后做，你不用做 GUI 真人点验，但要保证构建产物可运行）

## 验收标准

1. git 仓就绪、commit 干净、.gitignore 正确
2. CI workflow 文件语法正确（`actionlint` 或人工核对）；**本地模拟 CI 构建成功**（清掉 replace 依赖后 `go build` 在无本地主仓路径情况下可解析——用 `go mod download` 验证依赖闭合）
3. 测试全绿
4. 输出：选型说明（desktop 进主仓 vs 独立仓）、workflow 文件全文、测试结果
