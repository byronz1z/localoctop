package console

import (
	"fmt"
	"os/exec"
	"runtime"
)

// OpenBrowser launches the user's default browser at url. It is best-effort:
// the console keeps serving either way, and the tray menu offers the same
// action again.
func OpenBrowser(url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default: // linux and friends
		cmd = exec.Command("xdg-open", url)
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("console: open browser: %w", err)
	}
	go func() { _ = cmd.Wait() }()
	return nil
}
