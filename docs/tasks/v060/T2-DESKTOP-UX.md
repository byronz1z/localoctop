# T2 桌面客户端：真实状态 + 手动连接 + NSIS 安装器 + 开机自启（0.6.0）

仓库：D:/SelfHosted/localoctop，分支 feature/v0.6.0
范围：**只改 desktop/ 目录**。依赖 T1（桥核心 StatusDetail/OnStatusDetail）——**T1 合入后才开始**，届时读 docs/tasks/v060/T1-BRIDGE-HEARTBEAT.md 了解新接口。
先读 AGENTS.md、desktop/README.md、desktop/ 现有 app.go/frontend 结构再动手。

## 用户实测暴露的问题（这是需求来源，全部要修）

1. 状态卡"已连接"是假象——它显示最后一次尝试结果，桥掉线后仍显示已连接（真实事故：客户端绿标、服务器会话表为空）
2. "保存并连接"按钮无存在感（自动连接使其无用），且没有「断开」功能
3. 无安装逻辑：裸 EXE 解压到桌面运行，旁边落一堆文件
4. 无开机自启

## 任务

### 1. 状态卡真实化

- 使用 T1 新接口：Config 的 `OnStatusDetail(func(StatusDetail))` 或主动调 `Bridge.StatusDetail()`（每 3 秒轮询兜底，双保险）
- 状态卡五态渲染：
  - `已连接`（Connected=true，显示最后心跳 X 秒前，>90s 无 pong 显示黄色警示"心跳异常"）
  - `连接中…`（首次）
  - `重连中（第 N 次）`（Connected=false 且 attempt>0，附 LastError 摘要）
  - `已断开`（手动断开后）
  - `未配置`（无服务器地址/令牌）
- 前端事件桥：app.go 里把 StatusDetail 变化 emit 给前端（沿用现有 EventsEmit 模式，如 `bridge:status-detail`）

### 2. 手动连接/断开

- 「断开」按钮：cancel 桥 ctx，状态卡转"已断开"，不再自动重连
- 「连接」按钮：从配置重建桥（沿用 rebuildLocked 语义）
- 语义调整：应用启动仍自动连接（员工零操作），但手动断开后**保持断开**直到手动连接或改配置——断开是用户意志，自动连接不得覆盖
- 托盘菜单 tooltip 同步真实状态

### 3. NSIS 安装器

- Wails 内置：`wails build -nsis` 产出 `build/bin/localoctop-desktop-amd64-installer.exe`
- 核对 build/windows/installer/project.nsi 已有默认件，按需补：安装后自动启动一次、卸载时保留 %AppData%/localoctop 配置（用户数据不删）
- 安装器要求：currentUser 权限（不要管理员）、开始菜单+桌面快捷方式、带卸载器

### 4. 开机自启

- 设置页新增勾选「开机自动启动」
- 实现：写/删注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，值名 `localoctop-desktop`，值 `"EXE完整路径" --minimized`（golang.org/x/sys/windows/registry，已在依赖树内则直接用）
- `--minimized` 参数：带此参数启动时窗口不显示直接进托盘
- 状态持久化：存 config.json（appcfg 已有机制，新增 `autostart *bool` JSON 字段——注意 internal/appcfg 在仓库根，desktop 之外 shared；改动需兼容 headless：*bool+omitempty，并在根模块跑一次测试确认不回归）

### 5. 版本号统一 0.6.0

- desktop 版本显示、wails.json 的 productVersion 等全部对齐 0.6.0

## 质量门（必须全过并附原文）

GOROOT=D:/tmp/go GOPATH=D:/tmp/gopath GOTMPDIR=D:/tmp/gotmp，PATH 前置 /d/tmp/go/bin:/d/tmp/gopath/bin

```
cd desktop && go build ./... && go vet ./... && go test ./... -count=1
cd .. && go build ./... && go test ./internal/appcfg/ -count=1   # 根模块回归（appcfg 被改）
wails build -nsis     # 产物 build/bin/ 下 EXE + installer.exe
```

PE 断言（python）：
```
import struct
with open('build/bin/localoctop-desktop.exe','rb') as f:
    assert f.read(2)==b'MZ'
    f.seek(0x3C); e=struct.unpack('<I',f.read(4))[0]
    f.seek(e+24+68); assert struct.unpack('<H',f.read(2))[0]==2
```

## 交付

git add desktop/ + 根模块 appcfg 改动 + commit（中文信息，前缀 `feat(desktop):`），禁止 push。
报告：改动文件清单、五态渲染逻辑说明、注册表操作代码位置、质量门与 PE 断言输出原文、安装器产物路径、git log -1。
