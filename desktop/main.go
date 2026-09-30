package main

import (
	"embed"

	"github.com/byronz1z/localoctop"
	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	"github.com/wailsapp/wails/v2/pkg/options/windows"

	"github.com/byronz1z/localoctop/desktop/internal/tray"
)

//go:embed all:frontend/dist
var assets embed.FS

// localoctopVersion is the bridge core version shown in the tray tooltip.
func localoctopVersion() string { return localoctop.Version }

func main() {
	// Create an instance of the app structure
	app := NewApp()

	// Register the tray BEFORE wails.Run, from this main goroutine: systray's
	// Register() creates the tray's hidden window on the calling thread and
	// starts no loop of its own; this goroutine is already locked to the
	// process's main OS thread (winc's init runs on first import of the
	// Wails Windows frontend — which options/windows triggers — and calls
	// runtime.LockOSThread). Wails' own GetMessage(NULL) main loop then pumps
	// the tray window's messages alongside its own. See internal/tray's
	// package comment for the full verified reasoning.
	tray.Register(localoctopVersion(), tray.Actions{
		OpenWindow: app.showFromTray,
		Quit:       app.quitFromTray,
	})

	// Create application with options
	err := wails.Run(&options.App{
		Title:     "Octop 本地文件桥",
		Width:     1024,
		Height:    768,
		MinWidth:  640,
		MinHeight: 480,
		AssetServer: &assetserver.Options{
			Assets: assets,
		},
		BackgroundColour: &options.RGBA{R: 245, G: 246, B: 248, A: 1},
		OnStartup:        app.startup,
		OnShutdown:       app.shutdown,
		OnBeforeClose:    app.onBeforeClose, // close-to-tray (M3)
		Bind: []interface{}{
			app,
		},
		// windowsgui, no console flash; closing the window hides to tray
		// (the tray's "退出" or a confirmed quit is the real exit path).
		Windows: &windows.Options{
			WebviewIsTransparent: false,
			WindowIsTranslucent:  false,
		},
	})

	if err != nil {
		println("Error:", err.Error())
	}
}
