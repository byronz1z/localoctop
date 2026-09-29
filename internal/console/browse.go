package console

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// BrowseTimeout caps how long the native directory picker may stay open.
// After it fires, the picker subprocess is killed and the API reports an
// error — the UI falls back to manual typing and never hangs (task
// constraint: 120s timeout, silent fallback to manual entry).
const BrowseTimeout = 120 * time.Second

// picker shows a native "choose directory" dialog and returns the selected
// path. ok=false means the user cancelled. It is a variable so tests can
// inject a fake and never spawn a real dialog window.
type picker func(ctx context.Context) (path string, ok bool, err error)

var (
	// browseMu serializes dialogs: at most one picker at a time. A second
	// request while one is open gets 409 instead of a second window.
	browseMu sync.Mutex
	pickerMu sync.Mutex
	pickFn   picker = pickDirNative
)

// SetPicker overrides the native dialog implementation (tests only; pass
// nil to restore the native one).
func SetPicker(p picker) {
	pickerMu.Lock()
	if p == nil {
		pickFn = pickDirNative
	} else {
		pickFn = p
	}
	pickerMu.Unlock()
}

func currentPicker() picker {
	pickerMu.Lock()
	defer pickerMu.Unlock()
	return pickFn
}

// pickDirPS is the PowerShell script spawned by pickDirNative. It prints the
// chosen directory to stdout as UTF-8. Exit codes: 0 = picked, 2 = cancelled,
// 1 = error (e.g. WinForms unavailable). It blocks only on the user's choice.
const pickDirPS = `$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try { Add-Type -AssemblyName System.Windows.Forms } catch { exit 1 }
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Octop Local Bridge - 选择白名单目录'
$d.ShowNewFolderButton = $true
$r = $d.ShowDialog()
if ($r -ne [System.Windows.Forms.DialogResult]::OK) { exit 2 }
$p = $d.SelectedPath
if ([string]::IsNullOrWhiteSpace($p)) { exit 2 }
Write-Output $p
exit 0
`

// pickDirNative runs the Windows-native folder picker in a powershell.exe
// subprocess (system-provided, no extra dependencies; validated by the v0.2.1
// headless probe: ~250ms cold start, UTF-8 stdout, clean cancel semantics).
// The subprocess gets a minimal environment: no tokens, no config.
func pickDirNative(ctx context.Context) (string, bool, error) {
	if runtime.GOOS != "windows" {
		return "", false, errors.New("browse: the native directory picker is Windows-only; type the path manually")
	}
	script := filepath.Join(os.TempDir(), "octop-local-bridge-pickdir.ps1")
	if err := os.WriteFile(script, []byte(pickDirPS), 0o600); err != nil {
		return "", false, fmt.Errorf("browse: write picker script: %w", err)
	}
	cmd := exec.CommandContext(ctx, "powershell.exe",
		"-sta", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script)
	cmd.Env = minimalEnv()
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) && ee.ExitCode() == 2 {
			return "", false, nil // user cancelled: not an error
		}
		msg := strings.TrimSpace(string(ee.Stderr))
		if ctx.Err() != nil {
			return "", false, fmt.Errorf("browse: picker timed out after %s", BrowseTimeout)
		}
		if msg == "" {
			msg = err.Error()
		}
		return "", false, fmt.Errorf("browse: picker failed: %s", firstLine(msg))
	}
	path := strings.TrimSpace(string(out))
	if path == "" {
		return "", false, nil
	}
	return path, true, nil
}

// minimalEnv strips the child's environment down to what a Windows GUI
// subprocess needs, so LOCALFS_* tokens or config-like variables never leak
// into it (task constraint: 子进程不得继承 token/配置).
func minimalEnv() []string {
	keep := map[string]bool{
		"SYSTEMROOT": true, "WINDIR": true, "SYSTEMDRIVE": true, "COMSPEC": true,
		"PATH": true, "PATHEXT": true, "TEMP": true, "TMP": true, "OS": true,
		"USERPROFILE": true, "HOMEDRIVE": true, "HOMEPATH": true,
		"APPDATA": true, "LOCALAPPDATA": true, "NUMBER_OF_PROCESSORS": true,
	}
	var out []string
	for _, kv := range os.Environ() {
		i := strings.IndexByte(kv, '=')
		if i <= 0 {
			continue
		}
		if keep[strings.ToUpper(kv[:i])] {
			out = append(out, kv)
		}
	}
	return out
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}

// CommonDir is one "frequently used location" offered for one-click adding.
type CommonDir struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// commonDirs lists well-known directories under the user's profile that
// actually exist on this machine (existence-checked, duplicates collapsed):
// the profile root, Desktop, Documents, Downloads, plus their OneDrive
// redirects when OneDrive is active.
func commonDirs() []CommonDir {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return nil
	}
	cands := []CommonDir{
		{"用户目录", home},
		{"桌面", filepath.Join(home, "Desktop")},
		{"文档", filepath.Join(home, "Documents")},
		{"下载", filepath.Join(home, "Downloads")},
	}
	if od := os.Getenv("OneDrive"); od != "" {
		cands = append(cands,
			CommonDir{"桌面 (OneDrive)", filepath.Join(od, "Desktop")},
			CommonDir{"文档 (OneDrive)", filepath.Join(od, "Documents")},
		)
	}
	var out []CommonDir
	seen := map[string]bool{}
	for _, c := range cands {
		key := strings.ToLower(filepath.Clean(c.Path))
		if seen[key] {
			continue
		}
		st, err := os.Stat(c.Path)
		if err != nil || !st.IsDir() {
			continue
		}
		seen[key] = true
		out = append(out, c)
	}
	return out
}
