package localoctop

import "encoding/json"

// Wire protocol between the bridge client (Go) and the localoctop
// adapter (Python). JSON-RPC-flavored envelopes over a single WebSocket connection.
// The same shapes are mirrored in localoctop/protocol.py — keep both in sync.

// Request is an inbound call from the adapter: {id, method, params}.
type Request struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params map[string]any  `json:"params,omitempty"`
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
// adapter so tools/call maps 1:1 onto bridge methods. v0.5.0 aligns the
// catalog with the official MCP filesystem server (names kept for existing
// tools; new tools use their official names) plus user-requested delete and
// zip/unzip extensions.
const (
	MethodListDirectory          = "list_directory"
	MethodReadFile               = "read_file"
	MethodSearchFiles            = "search_files"
	MethodGetFileInfo            = "get_file_info"
	MethodWriteFile              = "write_file"
	MethodCreateDir              = "create_directory"
	MethodReadMediaFile          = "read_media_file"
	MethodReadMultipleFiles      = "read_multiple_files"
	MethodEditFile               = "edit_file"
	MethodListDirWithSizes       = "list_directory_with_sizes"
	MethodDirectoryTree          = "directory_tree"
	MethodMoveFile               = "move_file"
	MethodDeleteFile             = "delete_file"
	MethodRemoveDirectory        = "remove_directory"
	MethodZipFiles               = "zip_files"
	MethodUnzipFile              = "unzip_file"
	MethodListAllowedDirectories = "list_allowed_directories"
)

// Version is the product version, reported in register frames and shown on
// the console's about page. Preview releases start at v0.1.0.
const Version = "0.5.0"

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
	Size      int64  `json:"size"`     // total file size on disk
	Offset    int64  `json:"offset"`   // byte window start (0 = whole file)
	Bytes     int64  `json:"bytes"`    // bytes actually returned
	Truncated bool   `json:"truncated"`
}

// SearchHit is one match of search_files.
type SearchHit struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	MTime string `json:"mtime"`
	// Line/Preview are populated only for regex content search (v0.5.0):
	// first matching line (1-based) and up to 200 chars of that line.
	Line    int    `json:"line,omitempty"`
	Preview string `json:"preview,omitempty"`
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

// ---- v0.5.0 official-MCP-aligned result shapes ----

// MediaFileResult is the read_media_file payload: raw bytes base64'd with the
// guessed MIME type (images/audio/any file as embedded resource).
type MediaFileResult struct {
	Path   string `json:"path"`
	Base64 string `json:"base64"`
	MIME   string `json:"mime"`
	Size   int64  `json:"size"`
}

// MultiFileEntry is one element of a read_multiple_files batch. Failed reads
// report Error instead of aborting the whole call (official semantics).
type MultiFileEntry struct {
	Path     string `json:"path"`
	Text     string `json:"text,omitempty"`
	Encoding string `json:"encoding,omitempty"` // "utf-8" | "base64" when Text omitted
	Base64   string `json:"base64,omitempty"`
	Error    string `json:"error,omitempty"`
}

// ReadMultipleResult is the read_multiple_files payload.
type ReadMultipleResult struct {
	Files     []MultiFileEntry `json:"files"`
	Failed    int              `json:"failed"`
	Truncated bool             `json:"truncated"` // combined bytes hit the cap
}

// EditOp is one {oldText,newText} replacement inside edit_file.
type EditOp struct {
	OldText string `json:"oldText"`
	NewText string `json:"newText"`
}

// EditResult is the edit_file payload. Diff carries a unified-style preview;
// Applied=false when dryRun. Repeated oldText occurrences are an error
// unless all occurrences are replaced (count semantics per official spec).
type EditResult struct {
	Path         string `json:"path"`
	Applied      bool   `json:"applied"`
	DryRun       bool   `json:"dry_run"`
	EditsApplied int    `json:"edits_applied"`
	Matches      int    `json:"matches"` // total oldText occurrences replaced
	Diff         string `json:"diff"`    // git-style unified diff (context 3)
	SizeBefore   int64  `json:"size_before"`
	SizeAfter    int64  `json:"size_after"`
}

// SizeListEntry is one row of list_directory_with_sizes.
type SizeListEntry struct {
	Name string `json:"name"`
	Type string `json:"type"` // "file" | "dir"
	Size int64  `json:"size"`
}

// ListWithSizesResult is the list_directory_with_sizes payload.
type ListWithSizesResult struct {
	Path       string          `json:"path"`
	Entries    []SizeListEntry `json:"entries"`
	TotalFiles int             `json:"total_files"`
	TotalDirs  int             `json:"total_dirs"`
	TotalBytes int64           `json:"total_bytes"`
}

// TreeNode is one node of directory_tree (official shape: name/type/children).
type TreeNode struct {
	Name     string      `json:"name"`
	Type     string      `json:"type"` // "file" | "directory"
	Children []*TreeNode `json:"children,omitempty"`
}

// DirectoryTreeResult is the directory_tree payload.
type DirectoryTreeResult struct {
	Path      string      `json:"path"`
	Tree      []*TreeNode `json:"tree"`
	Truncated bool        `json:"truncated"`
}

// MoveResult is the move_file payload.
type MoveResult struct {
	Source      string `json:"source"`
	Destination string `json:"destination"`
	Moved       bool   `json:"moved"`
}

// DeleteResult is the delete_file / remove_directory payload. Permanent=false
// means the item went to the recycle bin (recoverable by the employee).
type DeleteResult struct {
	Path      string `json:"path"`
	Deleted   bool   `json:"deleted"`
	Permanent bool   `json:"permanent"`
	Recycle   bool   `json:"recycle_bin"`       // moved to OS recycle bin
	Entries   int    `json:"entries,omitempty"` // remove_directory: removed count
}

// ZipResult is the zip_files payload: the archive is built client-side under
// the whitelist and returned base64'd (size-capped).
type ZipResult struct {
	Archive   string `json:"archive"` // whitelist-relative path of the temp archive
	Base64    string `json:"base64"`
	Size      int64  `json:"size"` // archive bytes
	Entries   int    `json:"entries"`
	Skipped   int    `json:"skipped,omitempty"` // paths excluded by caps/permissions
	Truncated bool   `json:"truncated"`         // some requested paths skipped (cap)
}

// UnzipResult is the unzip_file payload.
type UnzipResult struct {
	Archive    string `json:"archive"`
	Dest       string `json:"dest"`
	Extracted  int    `json:"extracted"`
	TotalBytes int64  `json:"total_bytes"`
}

// AllowedDirsResult is the list_allowed_directories payload.
type AllowedDirsResult struct {
	Directories []string `json:"directories"`
	Note        string   `json:"note,omitempty"`
}
