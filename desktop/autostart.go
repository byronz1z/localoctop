// autostart.go — 开机自启的应用层接线（0.6.0 T2）：设置页勾选框背后的
// 绑定方法。平台相关读写见 autostart_windows.go / autostart_other.go；
// 本文件只负责把它接到 App 的配置持久化（appcfg.Autostart 镜像）上。
package main

import (
	"github.com/byronz1z/localoctop/internal/appcfg"
)

// autostartErrPayload is the SetAutostart return when the registry write
// succeeded but persisting the mirror failed: the frontend shows the
// warning but keeps the checkbox at the effective state.
type autostartErrPayload struct {
	Warning string `json:"warning"`
}

// GetAutostart reports the effective autostart state for the settings page.
// The registry wins over the config mirror (a value deleted outside the app
// must read "off"), and an absent config key does NOT default to false —
// it defers to the registry too, so an old install shows its real state.
// The second return is a warning string ("" = none): non-Windows builds
// and registry errors surface as "未知" rather than a wrong checkbox.
func (a *App) GetAutostart() (bool, string) {
	// Prefer the real registry state; fall back to the mirror only when the
	// registry is unreadable (non-Windows, hive error).
	on, err := autostartEnabled()
	if err == nil {
		return on, ""
	}
	a.mu.Lock()
	cfg := a.cfg
	a.mu.Unlock()
	if cfg.Autostart != nil {
		return *cfg.Autostart, err.Error()
	}
	return false, err.Error()
}

// SetAutostart toggles autostart: write/remove the HKCU Run entry first
// (the effective state), then persist the appcfg.Autostart mirror so the
// next start has a hint even if the registry probe fails. Registry failure
// = full error, checkbox reverts; mirror-persist failure = warning only,
// the toggle itself did work.
func (a *App) SetAutostart(on bool) error {
	if err := setAutostartEnabled(on); err != nil {
		return err
	}

	a.mu.Lock()
	cfg := a.cfg
	cfg.Autostart = &on
	err := appcfg.Save(cfg)
	if err == nil {
		a.cfg = cfg
	}
	a.mu.Unlock()
	return err
}
