package console

// The console is a single hand-written HTML page with inline CSS and vanilla
// JS — no build chain, no node, no external assets. embed keeps the EXE
// self-contained (task constraint: clone == build).

import _ "embed"

//go:embed console.html
var consoleHTML []byte
