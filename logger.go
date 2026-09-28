package localfsbridge

import (
	"fmt"
	"log"
	"os"
)

// Logger is the minimal logging surface the bridge needs. Plug in any
// structured logger (slog, zap, Wails runtime logger) by adapting to it.
type Logger interface {
	Debugf(format string, args ...any)
	Infof(format string, args ...any)
	Warnf(format string, args ...any)
	Errorf(format string, args ...any)
}

// StderrLogger is the default Logger: leveled output to stderr via log.Logger.
type StderrLogger struct {
	l     *log.Logger
	debug bool
}

// NewStderrLogger returns a Logger writing to stderr. Set debug to include
// Debugf lines.
func NewStderrLogger(debug bool) *StderrLogger {
	return &StderrLogger{
		l:     log.New(os.Stderr, "[localfsbridge] ", log.LstdFlags),
		debug: debug,
	}
}

func (s *StderrLogger) Debugf(format string, args ...any) {
	if s.debug {
		s.l.Printf("DEBUG "+format, args...)
	}
}

func (s *StderrLogger) Infof(format string, args ...any) {
	s.l.Printf("INFO  "+format, args...)
}

func (s *StderrLogger) Warnf(format string, args ...any) {
	s.l.Printf("WARN  "+format, args...)
}

func (s *StderrLogger) Errorf(format string, args ...any) {
	s.l.Printf("ERROR "+format, args...)
}

// NopLogger discards everything; useful in tests.
type NopLogger struct{}

func (NopLogger) Debugf(string, ...any) {}
func (NopLogger) Infof(string, ...any)  {}
func (NopLogger) Warnf(string, ...any)  {}
func (NopLogger) Errorf(string, ...any) {}

var _ Logger = (*StderrLogger)(nil)
var _ Logger = NopLogger{}

// sprintf is a tiny helper used across the package for message building.
func sprintf(format string, args ...any) string { return fmt.Sprintf(format, args...) }
