package localoctop

import "encoding/json"

// Wire protocol between the bridge client (Go) and the localoctop
// adapter (Python). JSON-RPC-flavored envelopes over a single WebSocket connection.
// The same shapes are mirrored in localoctop/protocol.py — keep both in sync.

// Request is an inbound call from the adapter: {id, method, params}.
type Request struct {
	ID     json.RawMessage          `json:"id"`
	Method string                   `json:"method"`
	Params map[string]any           `json:"params,omitempty"`
	// FrameType lets the adapter send control messages ("ping"/"register")
	// that are not tool calls. Empty means a tool call.
	FrameType string `json:"type,omitempty"`
}

// Response is the reply to a Request. Exactly one of Result/Error is set.
type Response struct {
	ID     json.RawMessage `json:"id"`
	Result any             `json:"result,omitempty"`
	Error  *Error          `json:"error,omitempty"`
}

// RegisterFrame is sent by the client immediately after the WebSocket opens.
// The adapter uses it to (re-)bind the connection to the token's user, which
// is what makes "重连后自动重新注册" work.
type RegisterFrame struct {
	Type       string   `json:"type"`
	Token      string   `json:"token"`
	ClientID   string   `json:"client_id"`
	Hostname   string   `json:"hostname,omitempty"`
	AllowedDir []string `json:"allowed_dirs,omitempty"`
	WriteMode  bool     `json:"write_enabled"`
	Version    string   `json:"version,omitempty"`
}

// Tool method names. These must match the MCP tool names exposed by the
// adapter so tools/call maps 1:1 onto bridge methods.
const (
	MethodListDirectory = "list_directory"
	MethodReadFile      = "read_file"
	MethodSearchFiles   = "search_files"
	MethodGetFileInfo   = "get_file_info"
	MethodWriteFile     = "write_file"
	MethodCreateDir     = "create_directory"
)

// Version is the product version, reported in register frames and shown on
// the console's about page. Preview releases start at v0.1.0.
const Version = "0.3.0"

// ---- Tool result shapes (mirrored by the adapter's MCP content blocks) ----

// DirEntry is one row of list_directory output.
type DirEntry struct {
	Name  string `json:"name"`
	Type  string `json:"type"` // "file" | "dir"
	Size  int64  `json:"size"`
	MTime string `json:"mtime"` // RFC3339
}

// ListDirectoryResult is the list_directory payload.
type ListDirectoryResult struct {
	Path    string     `json:"path"`
	Entries []DirEntry `json:"entries"`
}

// ReadFileResult is the read_file payload. Text files come back in Text with
// Truncated=false; binary files (or oversized text) are omitted in favor of
// Base64/size reporting decided by the handler.
type ReadFileResult struct {
	Path      string `json:"path"`
	Text      string `json:"text,omitempty"`
	Base64    string `json:"base64,omitempty"`
	Encoding  string `json:"encoding"` // "utf-8" | "base64"
	Size      int64  `json:"size"`
	Truncated bool   `json:"truncated"`
}

// SearchHit is one match of search_files.
type SearchHit struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	MTime string `json:"mtime"`
}

// SearchResult is the search_files payload.
type SearchResult struct {
	Pattern   string      `json:"pattern"`
	Path      string      `json:"path"`
	Matches   []SearchHit `json:"matches"`
	Truncated bool        `json:"truncated"` // hit the MaxResults cap
}

// FileInfo is the get_file_info payload.
type FileInfo struct {
	Path      string `json:"path"`
	Name      string `json:"name"`
	Type      string `json:"type"` // "file" | "dir"
	Size      int64  `json:"size"`
	MTime     string `json:"mtime"`
	ModTimeU  int64  `json:"mtime_unix"`
	ReadOnly  bool   `json:"read_only"`
	Symlink   bool   `json:"symlink"`
	MIMEGuess string `json:"mime,omitempty"`
}

// WriteResult is the write_file / create_directory payload.
type WriteResult struct {
	Path    string `json:"path"`
	Size    int64  `json:"size"`
	Created bool   `json:"created"`
}
