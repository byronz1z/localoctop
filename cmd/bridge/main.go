// Command bridge is a standalone runner for the localfsbridge client. It
// exists for two reasons:
//
//  1. Integration testing against the mock adapter (cmd/mockserver) or the
//     real mcp-localfs container, without the Wails shell.
//  2. A worked example of the single startup call the ZBSwork shell needs
//     (see localfsbridge.New / Bridge.Run).
//
// Usage:
//
//	go run ./cmd/bridge \
//	    -server "ws://127.0.0.1:18443/mcp-localfs/ws" \
//	    -token  dev-token \
//	    -dir    "~/Documents/ZBSwork" \
//	    -audit  "./audit.log"
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/byronz1z/localfsbridge"
)

func main() {
	server := flag.String("server", envOr("LOCALFS_SERVER_URL", "ws://127.0.0.1:18443/mcp-localfs/ws"), "adapter WebSocket URL")
	token := flag.String("token", envOr("LOCALFS_TOKEN", "dev-token"), "user token issued by the adapter")
	dirs := flag.String("dir", envOr("LOCALFS_DIRS", "~/Documents/ZBSwork"), "comma-separated whitelist directories")
	allowWrite := flag.Bool("write", envOr("LOCALFS_ALLOW_WRITE", "") == "1", "enable reserved write tools (default off)")
	audit := flag.String("audit", envOr("LOCALFS_AUDIT", ""), "audit log path (empty = stderr hook only)")
	verbose := flag.Bool("v", false, "debug logging")
	flag.Parse()

	cfg := localfsbridge.NewConfig()
	cfg.ServerURL = *server
	cfg.Token = *token
	for _, d := range strings.Split(*dirs, ",") {
		if d = strings.TrimSpace(d); d != "" {
			cfg.AllowedDirs = append(cfg.AllowedDirs, d)
		}
	}
	cfg.AllowWrite = *allowWrite
	cfg.AuditLogPath = *audit
	cfg.Logger = localfsbridge.NewStderrLogger(*verbose)
	cfg.OnStatus = func(connected bool, err error) {
		if connected {
			fmt.Fprintln(os.Stderr, "[status] connected")
		} else if err != nil {
			fmt.Fprintf(os.Stderr, "[status] disconnected: %v\n", err)
		} else {
			fmt.Fprintln(os.Stderr, "[status] disconnected")
		}
	}

	bridge, err := localfsbridge.New(cfg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	fmt.Fprintf(os.Stderr, "localfsbridge %s starting (client_id=%s, write=%v, dirs=%v)\n",
		localfsbridge.Version, bridge.ClientID(), *allowWrite, cfg.AllowedDirs)
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
