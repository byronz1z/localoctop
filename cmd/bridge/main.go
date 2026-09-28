// Command bridge is the Octop Local Bridge product binary: bridge core +
// embedded local web console + system tray, in one process and one EXE.
//
// Default (desktop) mode:
//
//	bridge            # loads the settings file, starts the console on
//	                  # 127.0.0.1:19880 (probing upward on conflict),
//	                  # opens the browser, and shows a tray icon
//	bridge --headless # pure bridge, no console, no tray: settings file or
//	                  # flags/env, stdout logging — the original CLI runner
//
// Headless flags (also honored via LOCALFS_* env vars):
//
//	bridge --headless \
//	    -server "ws://127.0.0.1:18443/mcp-localfs/ws" \
//	    -token  dev-token \
//	    -dir    "~/Documents/Octop" \
//	    -audit  "./audit.log"
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"

	"github.com/byronz1z/octop-local-bridge"
	"github.com/byronz1z/octop-local-bridge/internal/appcfg"
	"github.com/byronz1z/octop-local-bridge/internal/console"
	"github.com/byronz1z/octop-local-bridge/internal/tray"
)

func main() {
	headless := flag.Bool("headless", false, "pure bridge mode: no console, no tray (flags/env config)")
	server := flag.String("server", envOr("LOCALFS_SERVER_URL", ""), "headless: adapter WebSocket URL")
	token := flag.String("token", envOr("LOCALFS_TOKEN", ""), "headless: user token issued by the adapter")
	dirs := flag.String("dir", envOr("LOCALFS_DIRS", ""), "headless: comma-separated whitelist directories")
	allowWrite := flag.Bool("write", envOr("LOCALFS_ALLOW_WRITE", "") == "1", "headless: enable reserved write tools (default off)")
	audit := flag.String("audit", envOr("LOCALFS_AUDIT", ""), "headless: audit log path (empty = stderr hook only)")
	verbose := flag.Bool("v", false, "debug logging")
	flag.Parse()

	if *headless {
		runHeadless(*server, *token, *dirs, *allowWrite, *audit, *verbose)
		return
	}
	runDesktop(*verbose)
}

// ---- desktop mode: console + tray + bridge, one process ----

// appState is the mutable desktop state: the settings on disk and the
// running bridge. All access is under mu (console handlers call apply from
// their own goroutines).
type appState struct {
	mu     sync.Mutex
	cfg    appcfg.File
	bridge *octobridge.Bridge
	cancel context.CancelFunc // stops the current bridge's Run
}

func runDesktop(verbose bool) {
	cfg, existed, err := appcfg.Load()
	if err != nil {
		fmt.Fprintf(os.Stderr, "config: %v\n", err)
		os.Exit(1)
	}
	if !existed {
		if err := appcfg.Save(cfg); err != nil {
			// Not fatal: the console can still run and save later; warn only.
			fmt.Fprintf(os.Stderr, "config: cannot write defaults: %v\n", err)
		}
	}

	ln, err := console.Listen(cfg.ConsolePort, 0)
	if err != nil {
		fmt.Fprintf(os.Stderr, "console: %v\n", err)
		os.Exit(1)
	}
	consoleURL := console.Addr(ln)

	app := &appState{cfg: cfg}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// wire attaches the console/tray feeds to a bridge config. cid points at
	// the client id of the bridge being built; it is filled right after
	// octobridge.New and only read from OnStatus afterwards.
	var wire func(bc *octobridge.Config, cid *string)
	var srv *console.Server

	srv = console.New(cfg, func(next appcfg.File) error {
		app.mu.Lock()
		defer app.mu.Unlock()
		if err := appcfg.Save(next); err != nil {
			return err
		}
		app.cfg = next
		return rebuildLocked(ctx, app, verbose, wire, srv)
	})
	wire = func(bc *octobridge.Config, cid *string) {
		bc.OnStatus = func(connected bool, err error) {
			msg := ""
			if err != nil {
				msg = err.Error()
				fmt.Fprintf(os.Stderr, "[status] disconnected: %v\n", err)
			} else if connected {
				fmt.Fprintln(os.Stderr, "[status] connected")
			} else {
				fmt.Fprintln(os.Stderr, "[status] disconnected")
			}
			srv.PushStatus(connected, msg)
			tray.SetStatus(consoleURL, octobridge.Version, connected, *cid)
		}
		bc.OnAudit = func(ev octobridge.AuditEvent) {
			srv.PushAudit(ev)
		}
	}
	srv.SetAbout(console.About{
		Version:    octobridge.Version,
		ConsoleURL: consoleURL,
		ConfigPath: mustPath(appcfg.Path),
		AuditPath:  cfg.AuditLogPath,
	})

	// First bridge from the loaded settings. An unconfigured install builds
	// nothing: the console shows the onboarding page instead.
	app.mu.Lock()
	if err := rebuildLocked(ctx, app, verbose, wire, srv); err != nil {
		fmt.Fprintf(os.Stderr, "bridge: %v (configure via the console to fix)\n", err)
	}
	app.mu.Unlock()

	go func() {
		if err := srv.Serve(ln); err != nil {
			fmt.Fprintf(os.Stderr, "console: serve: %v\n", err)
		}
	}()

	if cfg.OpenBrowser {
		if err := console.OpenBrowser(consoleURL); err != nil {
			fmt.Fprintf(os.Stderr, "console: %v (open %s manually)\n", err, consoleURL)
		}
	}

	fmt.Fprintf(os.Stderr, "octop-local-bridge %s: console on %s (tray icon active; Ctrl+C to quit)\n",
		octobridge.Version, consoleURL)

	// Tray on the main goroutine (systray requirement on Windows); it blocks
	// until the tray "quit" item fires. Ctrl+C is handled after it returns.
	tray.Run(consoleURL, octobridge.Version, tray.Actions{
		OpenConsole: func() {
			if err := console.OpenBrowser(consoleURL); err != nil {
				fmt.Fprintf(os.Stderr, "console: %v\n", err)
			}
		},
		Quit: func() { stop() },
	})

	// Drain: stop the bridge, then the console.
	app.mu.Lock()
	if app.cancel != nil {
		app.cancel()
	}
	app.mu.Unlock()
	srv.Shutdown()
	fmt.Fprintln(os.Stderr, "octop-local-bridge stopped")
}

// rebuildLocked builds a Bridge from app.cfg and swaps it in, cancelling the
// previous one. Callers hold app.mu. A config that does not validate (e.g.
// the unconfigured first run) leaves the previous bridge running and
// returns the error so the console can surface it to the user.
func rebuildLocked(ctx context.Context, app *appState, verbose bool, wire func(*octobridge.Config, *string), srv *console.Server) error {
	bc, err := bridgeConfig(app.cfg, verbose)
	if err != nil {
		return err
	}
	var cid string
	if wire != nil {
		wire(&bc, &cid)
	}
	b, err := octobridge.New(bc)
	if err != nil {
		return err
	}
	cid = b.ClientID()
	if srv != nil {
		srv.SetStatusBase(cid, bc.ServerURL)
	}
	if app.cancel != nil {
		app.cancel() // stop the old bridge's Run loop
	}
	runCtx, runCancel := context.WithCancel(ctx)
	app.bridge, app.cancel = b, runCancel
	go b.Run(runCtx)
	return nil
}

// bridgeConfig maps the settings file onto the bridge core's Config, wiring
// the console's status/audit feeds.
func bridgeConfig(cfg appcfg.File, verbose bool) (octobridge.Config, error) {
	bc := octobridge.NewConfig()
	bc.ServerURL = cfg.ServerURL
	bc.Token = cfg.Token
	bc.AllowedDirs = cfg.EnabledDirs()
	bc.AllowWrite = cfg.AllowWrite
	bc.AuditLogPath = cfg.AuditLogPath
	bc.Logger = octobridge.NewStderrLogger(verbose)
	return bc, nil
}

// ---- headless mode: the original cmd/bridge behavior, flag/env driven ----

func runHeadless(server, token, dirs string, allowWrite bool, audit string, verbose bool) {
	// Fall back to the settings file for anything the flags leave empty, so a
	// configured desktop install also runs headless out of the box.
	if server == "" || token == "" || dirs == "" {
		if file, ok, err := appcfg.Load(); err == nil && ok {
			if server == "" {
				server = file.ServerURL
			}
			if token == "" {
				token = file.Token
			}
			if dirs == "" {
				dirs = strings.Join(file.EnabledDirs(), ",")
			}
			if audit == "" {
				audit = file.AuditLogPath
			}
		}
	}

	cfg := octobridge.NewConfig()
	cfg.ServerURL = server
	cfg.Token = token
	for _, d := range strings.Split(dirs, ",") {
		if d = strings.TrimSpace(d); d != "" {
			cfg.AllowedDirs = append(cfg.AllowedDirs, d)
		}
	}
	cfg.AllowWrite = allowWrite
	cfg.AuditLogPath = audit
	cfg.Logger = octobridge.NewStderrLogger(verbose)
	cfg.OnStatus = func(connected bool, err error) {
		if connected {
			fmt.Fprintln(os.Stderr, "[status] connected")
		} else if err != nil {
			fmt.Fprintf(os.Stderr, "[status] disconnected: %v\n", err)
		} else {
			fmt.Fprintln(os.Stderr, "[status] disconnected")
		}
	}

	bridge, err := octobridge.New(cfg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	fmt.Fprintf(os.Stderr, "octobridge %s starting (client_id=%s, write=%v, dirs=%v)\n",
		octobridge.Version, bridge.ClientID(), allowWrite, cfg.AllowedDirs)
	if err := bridge.Run(ctx); err != nil && ctx.Err() == nil {
		fmt.Fprintf(os.Stderr, "bridge stopped with error: %v\n", err)
		os.Exit(1)
	}
	fmt.Fprintln(os.Stderr, "bridge stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func mustPath(f func() (string, error)) string {
	p, err := f()
	if err != nil {
		return "(unknown)"
	}
	return p
}
