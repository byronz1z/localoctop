package localoctop

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

// Out-of-band file transfer (experiment branch).
//
// File bytes never ride the WebSocket control channel and never enter an MCP
// tool result. The adapter sends a small transfer_upload / transfer_download
// request over the WS; the bridge validates the path, answers immediately,
// and then moves the bytes over plain HTTP(S) to the same host in fixed-size
// chunks. Every chunk is acknowledged, so a dropped connection resumes from
// the last acknowledged offset instead of starting over, and the whole file
// is verified with SHA-256 on both ends before it is put in place.

const (
	MethodTransferUpload   = "transfer_upload"
	MethodTransferDownload = "transfer_download"
	MethodReadText         = "read_text"

	// 4 MiB chunks with a 5-minute budget each: a chunk still gets through on
	// a ~110 kbit/s uplink, and a failed chunk costs at most 4 MiB of resend.
	defaultTransferChunk = 4 << 20
	maxTransferChunk     = 16 << 20
	transferHTTPTimeout  = 300 * time.Second
	// A download stream with no bytes for this long is treated as dead.
	transferIdleTimeout = 120 * time.Second

	defaultTextLines = 200
	maxTextLines     = 2000
	maxTextBytes     = 48 << 10 // hard cap on text returned to the model per call
	maxTextLineBytes = 2000     // one very long line must not eat the whole budget
)

// transferBackoff is the wait before each retry of one chunk. The sum (~5 min)
// is how long a transfer rides out a network drop before giving up.
var transferBackoff = []time.Duration{
	1 * time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second,
	15 * time.Second, 30 * time.Second, 30 * time.Second, 30 * time.Second,
	60 * time.Second, 60 * time.Second, 60 * time.Second,
}

// TransferAccept is the immediate WS reply to a transfer request.
type TransferAccept struct {
	Accepted bool   `json:"accepted"`
	Path     string `json:"path"`
	Name     string `json:"name"`
	Size     int64  `json:"size"`
	MTime    string `json:"mtime"`
	MTimeU   int64  `json:"mtime_unix"`
}

// ReadTextResult is the read_text payload: a line window of a text file.
type ReadTextResult struct {
	Path       string `json:"path"`
	Size       int64  `json:"size"`
	StartLine  int    `json:"start_line"`
	EndLine    int    `json:"end_line"`
	TotalLines int    `json:"total_lines"` // -1 when the file is too big to count cheaply
	EOF        bool   `json:"eof"`
	Text       string `json:"text"`
	Clipped    int    `json:"clipped_lines"` // lines cut at maxTextLineBytes
}

type transferJob struct {
	id, ticket, baseURL string
}

// transferBaseURL maps the bridge WS URL to the HTTP origin of the adapter.
func (b *Bridge) transferBaseURL(urlPath string) (string, error) {
	u, err := url.Parse(b.cfg.ServerURL)
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "ws":
		u.Scheme = "http"
	case "wss":
		u.Scheme = "https"
	}
	if !strings.HasPrefix(urlPath, "/") || strings.Contains(urlPath, "..") {
		return "", fmt.Errorf("bad transfer url path %q", urlPath)
	}
	return u.Scheme + "://" + u.Host + urlPath, nil
}

func (b *Bridge) transferClient() *http.Client {
	return &http.Client{Timeout: transferHTTPTimeout}
}

func (b *Bridge) transferRequest(ctx context.Context, job transferJob, method, suffix string, body []byte, hdr map[string]string) (*http.Response, error) {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, job.baseURL+suffix, rd)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	req.Header.Set("X-Transfer-Ticket", job.ticket)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	return b.transferClient().Do(req)
}

// transferComplete tells the adapter how the transfer ended (best effort,
// retried a few times because this is the message the user-facing result
// depends on).
func (b *Bridge) transferComplete(job transferJob, payload map[string]any) {
	data, _ := json.Marshal(payload)
	for attempt := 0; attempt < 6; attempt++ {
		resp, err := b.transferRequest(context.Background(), job, http.MethodPost, "/complete", data,
			map[string]string{"Content-Type": "application/json"})
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode < 500 {
				return
			}
		}
		time.Sleep(transferBackoff[attempt])
	}
	b.log.Errorf("transfer %s: could not report completion to adapter", job.id)
}

// serverOffset asks the adapter how many bytes it has durably received.
func (b *Bridge) serverOffset(job transferJob) (int64, bool, error) {
	resp, err := b.transferRequest(context.Background(), job, http.MethodGet, "", nil, nil)
	if err != nil {
		return 0, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusGone {
		return 0, true, fmt.Errorf("adapter no longer knows this transfer (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		return 0, false, fmt.Errorf("status HTTP %d", resp.StatusCode)
	}
	var st struct {
		Received int64  `json:"received"`
		State    string `json:"state"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		return 0, false, err
	}
	if st.State == "failed" || st.State == "cancelled" {
		return 0, true, fmt.Errorf("adapter marked the transfer %s", st.State)
	}
	return st.Received, false, nil
}

// ---- transfer_upload: local file -> cloud ----

func (b *Bridge) transferUpload(params map[string]any) (*TransferAccept, *BridgeError) {
	abs, berr := b.tools.resolveAny(getString(params, "path", ""))
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("file not found: "+relDisplay(b.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("stat failed", err)
	}
	if st.IsDir() {
		return nil, errInvalidParams("path is a directory; send files one at a time")
	}
	maxBytes := int64(toInt(params["max_bytes"], 0))
	if v, ok := params["max_bytes"].(float64); ok {
		maxBytes = int64(v)
	}
	if maxBytes > 0 && st.Size() > maxBytes {
		return nil, errTooLarge(sprintf("file is %d bytes, larger than the %d-byte transfer limit", st.Size(), maxBytes))
	}
	acc := &TransferAccept{
		Accepted: true,
		Path:     relDisplay(b.cfg.AllowedDirs, abs),
		Name:     st.Name(),
		Size:     st.Size(),
		MTime:    st.ModTime().UTC().Format(time.RFC3339),
		MTimeU:   st.ModTime().Unix(),
	}
	if getBool(params, "stat_only", false) {
		return acc, nil
	}
	base, uerr := b.transferBaseURL(getString(params, "url_path", ""))
	if uerr != nil {
		return nil, errInvalidParams(uerr.Error())
	}
	job := transferJob{id: getString(params, "transfer_id", ""), ticket: getString(params, "ticket", ""), baseURL: base}
	if job.id == "" || job.ticket == "" {
		return nil, errInvalidParams("transfer_id and ticket are required")
	}
	chunk := int64(toInt(params["chunk_bytes"], defaultTransferChunk))
	if chunk <= 0 || chunk > maxTransferChunk {
		chunk = defaultTransferChunk
	}
	go b.runUpload(job, abs, st, chunk)
	return acc, nil
}

func (b *Bridge) runUpload(job transferJob, abs string, st0 os.FileInfo, chunk int64) {
	start := time.Now()
	fail := func(msg string) {
		b.log.Warnf("transfer %s upload failed: %s", job.id, msg)
		b.transferComplete(job, map[string]any{"ok": false, "error": msg})
		b.audit.Record(AuditEvent{Method: MethodTransferUpload, Path: relDisplay(b.cfg.AllowedDirs, abs),
			Decision: "error", Code: CodeInternal, Detail: msg, Duration: time.Since(start).String(), ClientID: b.clientID})
	}
	f, err := os.Open(abs) // #nosec G304 - path validated by guard
	if err != nil {
		fail("cannot open file: " + err.Error())
		return
	}
	defer f.Close()

	size := st0.Size()
	hasher := sha256.New()
	buf := make([]byte, chunk)
	var offset int64
	for offset < size {
		n := chunk
		if size-offset < n {
			n = size - offset
		}
		// Notice an edit as soon as it happens instead of after the last
		// byte: on a slow link that saves minutes of pointless upload.
		if cur, serr := os.Stat(abs); serr != nil || cur.Size() != size || !cur.ModTime().Equal(st0.ModTime()) {
			fail("the local file was modified during the transfer; please fetch it again")
			return
		}
		if _, err := io.ReadFull(io.NewSectionReader(f, offset, n), buf[:n]); err != nil {
			fail("file changed or became unreadable while sending: " + err.Error())
			return
		}
		if ferr := b.putChunk(job, offset, buf[:n]); ferr != nil {
			fail(ferr.Error())
			return
		}
		hasher.Write(buf[:n])
		offset += n
	}
	// The file must not have changed underneath us, or the cloud copy would
	// be a mix of two versions.
	st1, err := os.Stat(abs)
	if err != nil || st1.Size() != size || !st1.ModTime().Equal(st0.ModTime()) {
		fail("the local file was modified during the transfer; please fetch it again")
		return
	}
	b.transferComplete(job, map[string]any{
		"ok": true, "size": size, "sha256": hex.EncodeToString(hasher.Sum(nil)),
	})
	b.audit.Record(AuditEvent{Method: MethodTransferUpload, Path: relDisplay(b.cfg.AllowedDirs, abs),
		Decision: "allow", Code: CodeOK, Detail: sprintf("%d bytes sent to cloud", size),
		Duration: time.Since(start).String(), ClientID: b.clientID})
}

// putChunk sends one chunk and returns only when the adapter has
// acknowledged exactly that chunk, or when retries are exhausted.
func (b *Bridge) putChunk(job transferJob, offset int64, data []byte) error {
	want := offset + int64(len(data))
	var lastErr error
	for attempt := 0; ; attempt++ {
		resp, err := b.transferRequest(context.Background(), job, http.MethodPut,
			sprintf("/chunk?offset=%d", offset), data, map[string]string{"Content-Type": "application/octet-stream"})
		if err == nil {
			code := resp.StatusCode
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			switch {
			case code == http.StatusOK:
				return nil
			case code == http.StatusNotFound || code == http.StatusGone || code == http.StatusForbidden ||
				code == http.StatusRequestEntityTooLarge || code == http.StatusInsufficientStorage:
				return fmt.Errorf("adapter refused the transfer (HTTP %d)", code)
			}
			lastErr = fmt.Errorf("HTTP %d", code)
		} else {
			lastErr = err
		}
		// The chunk may have landed even though we did not see the answer
		// (connection cut after the adapter wrote it). Ask before resending.
		if got, fatal, serr := b.serverOffset(job); serr == nil {
			if got == want {
				return nil
			}
			if got != offset {
				return fmt.Errorf("adapter offset %d does not match local offset %d", got, offset)
			}
		} else if fatal {
			return serr
		}
		if attempt >= len(transferBackoff) {
			return fmt.Errorf("network did not recover after %d retries: %v", attempt, lastErr)
		}
		b.log.Warnf("transfer %s: chunk at %d failed (%v), retry %d", job.id, offset, lastErr, attempt+1)
		time.Sleep(transferBackoff[attempt])
	}
}

// ---- transfer_download: cloud file -> local ----

func (b *Bridge) transferDownload(params map[string]any) (*TransferAccept, *BridgeError) {
	if !b.cfg.AllowWrite {
		return nil, errWriteDisabled()
	}
	abs, berr := b.tools.resolveAny(getString(params, "path", ""))
	if berr != nil {
		return nil, berr
	}
	if st, err := os.Stat(abs); err == nil && st.IsDir() {
		return nil, errInvalidParams("destination is a directory; give a file path")
	}
	base, uerr := b.transferBaseURL(getString(params, "url_path", ""))
	if uerr != nil {
		return nil, errInvalidParams(uerr.Error())
	}
	job := transferJob{id: getString(params, "transfer_id", ""), ticket: getString(params, "ticket", ""), baseURL: base}
	if job.id == "" || job.ticket == "" {
		return nil, errInvalidParams("transfer_id and ticket are required")
	}
	var size int64
	if v, ok := params["size"].(float64); ok {
		size = int64(v)
	}
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return nil, errInternal("create destination folder failed", err)
	}
	go b.runDownload(job, abs, size, getString(params, "sha256", ""), getBool(params, "overwrite", false))
	return &TransferAccept{Accepted: true, Path: relDisplay(b.cfg.AllowedDirs, abs), Name: filepath.Base(abs), Size: size}, nil
}

func (b *Bridge) runDownload(job transferJob, abs string, size int64, wantSHA string, overwrite bool) {
	start := time.Now()
	// The temp name is unique per transfer and created O_EXCL: two downloads
	// landing in the same folder never share one temp file, and a file that
	// happens to sit at a candidate name is never opened or clobbered.
	part := sprintf("%s.localoctop-part-%s", abs, job.id)
	fail := func(msg string) {
		os.Remove(part)
		b.log.Warnf("transfer %s download failed: %s", job.id, msg)
		b.transferComplete(job, map[string]any{"ok": false, "error": msg})
		b.audit.Record(AuditEvent{Method: MethodTransferDownload, Path: relDisplay(b.cfg.AllowedDirs, abs),
			Decision: "error", Code: CodeInternal, Detail: msg, Duration: time.Since(start).String(), ClientID: b.clientID})
	}
	out, err := os.OpenFile(part, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644) // #nosec G304,G302 - validated path
	if err != nil {
		fail("cannot create file in the destination folder: " + err.Error())
		return
	}
	hasher := sha256.New()
	var offset int64
	attempt := 0
	for offset < size || (size == 0 && attempt == 0) {
		n, err := b.getRange(job, offset, io.MultiWriter(out, hasher))
		offset += n
		if err == nil {
			break
		}
		if n > 0 {
			attempt = 0 // progress was made: the network is alive again
		}
		if attempt >= len(transferBackoff) {
			out.Close()
			fail(sprintf("network did not recover after %d retries: %v", attempt, err))
			return
		}
		if _, fatal, serr := b.serverOffset(job); fatal {
			out.Close()
			fail(serr.Error())
			return
		}
		b.log.Warnf("transfer %s: download interrupted at %d (%v), retry %d", job.id, offset, err, attempt+1)
		time.Sleep(transferBackoff[attempt])
		attempt++
	}
	if err := out.Sync(); err != nil {
		out.Close()
		fail("could not flush file to disk (disk full?): " + err.Error())
		return
	}
	out.Close()
	got := hex.EncodeToString(hasher.Sum(nil))
	if offset != size || (wantSHA != "" && got != wantSHA) {
		fail(sprintf("integrity check failed (got %d bytes, expected %d)", offset, size))
		return
	}
	final := abs
	replaced := false
	if _, err := os.Stat(final); err == nil && overwrite {
		// Keep the old version recoverable: recycle bin when the platform
		// has one, otherwise a timestamped sibling.
		if ok, terr := trashPath(final); terr != nil || !ok {
			bak := final + time.Now().Format(".bak-20060102-150405")
			if rerr := os.Rename(final, bak); rerr != nil {
				fail("could not set aside the existing file: " + rerr.Error())
				return
			}
		}
		replaced = true
	}
	if !replaced {
		// Claim a free name atomically (link the temp file to the candidate
		// name, O_EXCL-style): no check-then-rename race, so two deliveries
		// aiming at the same path each get their own intact file.
		claimed, err := claimFreeName(part, final)
		if err != nil {
			fail("could not put the file in place: " + err.Error())
			return
		}
		final = claimed
	} else if err := os.Rename(part, final); err != nil {
		fail("could not put the file in place: " + err.Error())
		return
	}
	rel := relDisplay(b.cfg.AllowedDirs, final)
	b.transferComplete(job, map[string]any{"ok": true, "size": size, "sha256": got, "path": rel, "replaced": replaced})
	b.audit.Record(AuditEvent{Method: MethodTransferDownload, Path: rel, Decision: "allow", Code: CodeOK,
		Detail: sprintf("%d bytes received from cloud", size), Duration: time.Since(start).String(), ClientID: b.clientID})
}

// getRange streams bytes [offset, end) of the cloud file into w and returns
// how many bytes were written, even when the stream breaks half-way.
func (b *Bridge) getRange(job transferJob, offset int64, w io.Writer) (int64, error) {
	req, err := http.NewRequest(http.MethodGet, job.baseURL+"/content", nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.Token)
	req.Header.Set("X-Transfer-Ticket", job.ticket)
	req.Header.Set("Range", sprintf("bytes=%d-", offset))
	// No overall timeout here: a multi-GB body legitimately takes minutes.
	// A stalled connection is caught by the idle watchdog below instead.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	resp, err := (&http.Client{}).Do(req.WithContext(ctx))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPartialContent && resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	idle := time.AfterFunc(transferIdleTimeout, cancel)
	defer idle.Stop()
	var n int64
	buf := make([]byte, 256<<10)
	for {
		m, rerr := resp.Body.Read(buf)
		if m > 0 {
			idle.Reset(transferIdleTimeout)
			if _, werr := w.Write(buf[:m]); werr != nil {
				return n, fmt.Errorf("local write failed (disk full?): %w", werr)
			}
			n += int64(m)
		}
		if rerr == io.EOF {
			return n, nil
		}
		if rerr != nil {
			return n, rerr
		}
	}
}

// claimFreeName puts `part` in place at the first free candidate among
// "name.ext", "name (1).ext", "name (2).ext", ... and returns the claimed
// path. Claiming is atomic — os.Link fails with EEXIST when the name is
// taken — so two concurrent deliveries for the same target each end up with
// their own intact file instead of racing one rename. After a successful
// claim the temp path is unlinked (the data keeps living under the claimed
// name; a stray unlink failure only leaves a hard-linked duplicate, which
// is removed best effort).
func claimFreeName(part, target string) (string, error) {
	ext := filepath.Ext(target)
	stem := strings.TrimSuffix(target, ext)
	candidates := make([]string, 0, 1001)
	candidates = append(candidates, target)
	for i := 1; i < 1000; i++ {
		candidates = append(candidates, sprintf("%s (%d)%s", stem, i, ext))
	}
	candidates = append(candidates, stem+time.Now().Format(" 20060102-150405")+ext)
	var lastErr error
	for _, cand := range candidates {
		err := os.Link(part, cand)
		if err == nil {
			// The data now lives under the claimed name; dropping the temp
			// alias is best effort (a leftover alias is only a duplicate
			// name for the same bytes, never data loss).
			os.Remove(part)
			return cand, nil
		}
		if os.IsExist(err) {
			continue
		}
		lastErr = err
	}
	if lastErr != nil {
		return "", lastErr
	}
	return "", fmt.Errorf("no free name for %q", target)
}

// ---- read_text: bounded, line-based text reading ----

// readText returns a window of whole lines. Unlike the byte-window read_file
// it never splits a multi-byte character, never returns more than
// maxTextBytes, and tells the caller exactly where the next window starts.
func (t *tools) readText(params map[string]any) (*ReadTextResult, *BridgeError) {
	abs, berr := t.resolveAny(getString(params, "path", ""))
	if berr != nil {
		return nil, berr
	}
	st, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNotFound("file not found: "+relDisplay(t.cfg.AllowedDirs, abs), err)
		}
		return nil, errInternal("stat failed", err)
	}
	if st.IsDir() {
		return nil, errInvalidParams("path is a directory; use list_directory")
	}
	startLine := getInt(params, "start_line", 1)
	if startLine < 1 {
		startLine = 1
	}
	maxLines := getInt(params, "max_lines", defaultTextLines)
	if maxLines < 1 || maxLines > maxTextLines {
		maxLines = defaultTextLines
	}
	f, oerr := os.Open(abs) // #nosec G304 - path validated by guard
	if oerr != nil {
		return nil, errInternal("open failed", oerr)
	}
	defer f.Close()

	head := make([]byte, 8192)
	hn, _ := io.ReadFull(f, head)
	if hasBinaryMarker(head[:hn]) {
		return nil, errInvalidParams(sprintf(
			"%s is a binary file (%d bytes) and cannot be read as text; use fetch_to_cloud to send it to the cloud workspace and process it there",
			relDisplay(t.cfg.AllowedDirs, abs), st.Size()))
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, errInternal("seek failed", err)
	}

	res := &ReadTextResult{Path: relDisplay(t.cfg.AllowedDirs, abs), Size: st.Size(), StartLine: startLine, TotalLines: -1}
	rd := bufio.NewReaderSize(f, 256<<10)
	var sb strings.Builder
	line, taken := 0, 0
	stopped := false
	for {
		s, clipped, ok, rerr := nextLine(rd)
		if rerr != nil && rerr != io.EOF {
			return nil, errInternal("read failed", rerr)
		}
		if ok {
			line++
			if line >= startLine {
				if !utf8.Valid(s) {
					return nil, errInvalidParams("file is not UTF-8 text; use fetch_to_cloud and process it in the cloud workspace")
				}
				if taken >= maxLines || sb.Len()+len(s) > maxTextBytes {
					stopped = true // this line was read but not delivered
					break
				}
				sb.Write(s)
				if clipped {
					sb.WriteString(" …[line clipped]\n")
					res.Clipped++
				}
				taken++
			}
		}
		if rerr == io.EOF {
			res.EOF = true
			res.TotalLines = line
			break
		}
	}
	res.EndLine = startLine + taken - 1
	res.Text = sb.String()
	// Counting every line of a huge file just to print a total would make
	// the call slow; only finish the count for reasonably small files.
	const countBudget = 64 << 20
	if stopped && st.Size() <= countBudget {
		total := line
		for {
			_, _, ok, rerr := nextLine(rd)
			if ok {
				total++
			}
			if rerr != nil {
				break
			}
		}
		res.TotalLines = total
	}
	return res, nil
}

// nextLine reads one logical line of any length but keeps at most
// maxTextLineBytes of it (cut on a character boundary). ok is false only when
// nothing was left to read; err is io.EOF on the last line.
func nextLine(rd *bufio.Reader) (line []byte, clipped, ok bool, err error) {
	for {
		chunk, rerr := rd.ReadSlice('\n')
		if len(chunk) > 0 {
			ok = true
			if len(line) <= maxTextLineBytes {
				line = append(line, chunk...)
			} else {
				clipped = true
			}
		}
		if rerr == bufio.ErrBufferFull {
			continue
		}
		if len(line) > maxTextLineBytes {
			line = line[:maxTextLineBytes]
			for len(line) > 0 && !utf8.Valid(line) {
				line = line[:len(line)-1]
			}
			clipped = true
		}
		return line, clipped, ok, rerr
	}
}
