//go:build !windows

// autostart_other.go — 非 Windows 宿主（go build ./... / go vet 跑 CI 的
// linux runner）下开机自启不可用：桩实现让根模块编译通过并明确报错，
// 不产生行为。桌面产品只发 Windows 包（见 CI release.yml）。
package main

func setAutostartEnabled(enable bool) error {
	if enable {
		return errAutostartUnsupported
	}
	return nil // "取消自启"在无自启的平台上是幂等 no-op
}

func autostartEnabled() (bool, error) {
	return false, errAutostartUnsupported
}

type autostartErr string

func (e autostartErr) Error() string { return string(e) }

const errAutostartUnsupported = autostartErr("开机自启仅支持 Windows")
