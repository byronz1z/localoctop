//go:build windows

// autostart_windows.go — 开机自启（0.6.0 T2）：读写 HKCU Run 注册表。
//
// 写入键：HKCU\Software\Microsoft\Windows\CurrentVersion\Run
// 值名：localoctop-desktop
// 值：`"C:\...\localoctop-desktop.exe" --minimized`（EXE 路径带引号防含空格；
// --minimized 让开机启动直接进托盘，不弹窗口）
//
// 只动 HKCU（当前用户），无需管理员权限，与 NSIS 的 currentUser 安装一致。
// 用 golang.org/x/sys/windows/registry（已在依赖树内，v0.30.0）。
package main

import (
	"fmt"
	"os"

	"golang.org/x/sys/windows/registry"
)

const (
	autostartRunKey  = `Software\Microsoft\Windows\CurrentVersion\Run`
	autostartRunName = "localoctop-desktop"
)

// autostartCommand builds the Run value for the given exe path:
// `"C:\path\localoctop-desktop.exe" --minimized`.
func autostartCommand(exe string) string {
	return fmt.Sprintf(`"%s" --minimized`, exe)
}

// setAutostartEnabled creates or removes the HKCU Run entry. enable=false
// deletes the value (DeleteValue is a no-op when absent). A true value is
// always rewritten so a stale path (moved install) heals on next toggle.
func setAutostartEnabled(enable bool) error {
	k, err := registry.OpenKey(registry.CURRENT_USER, autostartRunKey, registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("打开 HKCU Run 键: %w", err)
	}
	defer k.Close()

	if !enable {
		return k.DeleteValue(autostartRunName)
	}
	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("获取程序路径: %w", err)
	}
	return k.SetStringValue(autostartRunName, autostartCommand(exe))
}

// autostartEnabled reports the REAL state by reading the registry (not the
// config mirror): if the user edited/deleted the value outside the app, the
// checkbox must reflect that. Bool + separate error so callers can render
// "未知" instead of guessing on failure.
func autostartEnabled() (bool, error) {
	k, err := registry.OpenKey(registry.CURRENT_USER, autostartRunKey, registry.QUERY_VALUE)
	if err != nil {
		return false, fmt.Errorf("打开 HKCU Run 键: %w", err)
	}
	defer k.Close()

	v, _, err := k.GetStringValue(autostartRunName)
	if err == registry.ErrNotExist {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("读取 %s 值: %w", autostartRunName, err)
	}
	return v != "", nil
}
