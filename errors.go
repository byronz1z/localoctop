package localoctop

import "fmt"

// Wire error codes shared with the localoctop adapter. Keep in sync with
// localoctop/protocol.py on the server side.
const (
	CodeOK             = 0
	CodeInvalidParams  = -32602 // JSON-RPC: malformed / missing params
	CodeMethodNotFound = -32601 // JSON-RPC: unknown tool
	CodeInternal       = -32000 // unexpected server(client)-side failure
	CodeNotAllowed     = 4001   // path outside whitelist / traversal / symlink escape
	CodeNotFound       = 4002   // file or directory does not exist
	CodeTooLarge       = 4003   // payload exceeds configured size limit
	CodeWriteDisabled  = 4004   // write tool invoked while AllowWrite=false
	CodeTimeout        = 4005   // operation exceeded RequestWait
)

// Error is the structured error carried on the wire in Response.Error.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *Error) Error() string { return fmt.Sprintf("bridge error %d: %s", e.Code, e.Message) }

// BridgeError is the in-process error type returned by handlers. It converts
// to the wire Error via ToWire.
type BridgeError struct {
	Code    int
	Message string
	Err     error // optional wrapped cause (never sent on the wire)
}

func (e *BridgeError) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("%s: %v", e.Message, e.Err)
	}
	return e.Message
}

func (e *BridgeError) Unwrap() error { return e.Err }

// ToWire produces the serializable form. Internal causes are intentionally
// dropped so they never leak to the caller.
func (e *BridgeError) ToWire() *Error { return &Error{Code: e.Code, Message: e.Message} }

// Constructors for the error families used by the tool handlers.

func errNotAllowed(msg string) *BridgeError {
	return &BridgeError{Code: CodeNotAllowed, Message: msg}
}

func errNotFound(msg string, err error) *BridgeError {
	return &BridgeError{Code: CodeNotFound, Message: msg, Err: err}
}

func errTooLarge(msg string) *BridgeError {
	return &BridgeError{Code: CodeTooLarge, Message: msg}
}

func errWriteDisabled() *BridgeError {
	return &BridgeError{Code: CodeWriteDisabled, Message: "write operations are disabled on this client"}
}

func errInvalidParams(msg string) *BridgeError {
	return &BridgeError{Code: CodeInvalidParams, Message: msg}
}

func errMethodNotFound(method string) *BridgeError {
	return &BridgeError{Code: CodeMethodNotFound, Message: "unknown method: " + method}
}

func errInternal(msg string, err error) *BridgeError {
	return &BridgeError{Code: CodeInternal, Message: msg, Err: err}
}
