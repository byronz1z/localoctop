# 发版检查单（RELEASE_CHECKLIST）

> 铁律：以下每一项勾完才允许打 tag。任何一项失败 = 停止发版，修复后重走全单。
> 版本号规则：全仓单一版本号。bug 修 +0.0.1，功能 +0.1.0，破坏性 +1.0.0。tag 同时打 `v<X.Y.Z>`（服务器/桥）与 `desktop-v<X.Y.Z>`（桌面安装包），两者必须同号发布。

## A. 版本一致性
- [ ] bridge/、server/、desktop/ 三处版本声明已同步为本版号
- [ ] VERSIONS.md 已更新本版三组件对应关系
- [ ] CHANGELOG.md 已填写本版变更（唯一权威，发版后不可改）

## B. 质量门（CI 与本地双跑）
- [ ] `go build ./...` 零错误
- [ ] `go vet ./...` 零告警
- [ ] `go test ./...` 全绿（含 appcfg/console/根包）
- [ ] desktop/：`go build/vet/test ./...` 全绿
- [ ] `wails build` 成功，产物 PE subsystem == 2（GUI，无控制台黑框）
- [ ] server/：pytest 全绿（如有改动）
- [ ] e2e 脚本全绿（跨桥-服务器全链路）

## C. 实机验收（中枢自测，不过关不交用户）
- [ ] setup.exe 安装 → 开始菜单/桌面快捷方式生成 → 启动正常
- [ ] 自动连接成功，挂机 ≥10 分钟不掉线（心跳生效）
- [ ] 断网 30 秒 → 恢复 → ≤10 秒自动重连，状态卡如实显示重连过程
- [ ] AI 经 MCP 读/写白名单文件成功
- [ ] 卸载干净（程序/快捷方式/自启项移除，配置文件按用户选择保留）
- [ ] headless zip 冒烟：解压运行无黑框、healthz OK

## D. 发布动作
- [ ] CI 两条流水线全绿，Release 产物齐全（zip+sha256 / setup.exe）
- [ ] 中枢下载 CI 产物独立复核（sha256 + PE + 冒烟）
- [ ] 生产部署：**单独向用户报备并获批后**执行，完成后回执
- [ ] ops 仓 compose 版本号同步升级并 push
