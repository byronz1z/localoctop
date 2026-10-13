package localoctop

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"
)

// ---- readText: bounded, line-based text reading ----

func TestReadTextLineWindow(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	body := "l1\nl2\nl3\nl4\nl5\n"
	must(t, os.WriteFile(filepath.Join(root, "win.txt"), []byte(body), 0o644))

	res, berr := tl.readText(map[string]any{"path": "win.txt", "start_line": 2, "max_lines": 2})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if res.StartLine != 2 || res.EndLine != 3 || res.Text != "l2\nl3\n" {
		t.Fatalf("bad window: %+v text=%q", res, res.Text)
	}
	if res.EOF {
		t.Fatal("window of a 5-line file at lines 2-3 must not report EOF")
	}
	if res.TotalLines != 5 {
		t.Fatalf("small file should be fully counted, got %d", res.TotalLines)
	}

	// Asking past the end yields an empty window at EOF.
	res, berr = tl.readText(map[string]any{"path": "win.txt", "start_line": 10})
	if berr != nil {
		t.Fatalf("readText past EOF failed: %v", berr)
	}
	if !res.EOF || res.Text != "" || res.TotalLines != 5 {
		t.Fatalf("past-EOF window wrong: %+v", res)
	}
	if res.EndLine != 9 { // start + taken - 1 with taken == 0
		t.Fatalf("empty window end_line should be start-1, got %d", res.EndLine)
	}
}

func TestReadTextDefaultsAndCaps(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.WriteFile(filepath.Join(root, "one.txt"), []byte("only\n"), 0o644))

	// Out-of-range start_line clamps to 1; bogus max_lines falls back to the
	// default window.
	res, berr := tl.readText(map[string]any{"path": "one.txt", "start_line": -5, "max_lines": 0})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if res.StartLine != 1 || res.Text != "only\n" || !res.EOF {
		t.Fatalf("clamp/default wrong: %+v", res)
	}
}

func TestReadTextUTF8NotSplit(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	// Each line is long but made of multi-byte runes; readText must never cut
	// a character in half, even when the byte budget forces a stop.
	var sb strings.Builder
	for i := 0; i < 500; i++ {
		sb.WriteString(strings.Repeat("中", 100) + "\n")
	}
	must(t, os.WriteFile(filepath.Join(root, "cjk.txt"), []byte(sb.String()), 0o644))

	res, berr := tl.readText(map[string]any{"path": "cjk.txt"})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if !utf8.ValidString(res.Text) {
		t.Fatal("returned text is not valid UTF-8 — a multi-byte character was split")
	}
	if len(res.Text) > maxTextBytes {
		t.Fatalf("text budget exceeded: %d > %d", len(res.Text), maxTextBytes)
	}
	if res.EOF {
		t.Fatal("a 150 KB file must not fit into one 48 KB window")
	}
	if res.TotalLines != 500 {
		t.Fatalf("small file should be fully counted, got %d", res.TotalLines)
	}
}

func TestReadTextBinaryRefused(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	bin := append([]byte("MZ"), make([]byte, 100)...) // NUL bytes early on
	must(t, os.WriteFile(filepath.Join(root, "app.exe"), bin, 0o644))

	_, berr := tl.readText(map[string]any{"path": "app.exe"})
	if berr == nil {
		t.Fatal("binary file must be refused")
	}
	if berr.Code != CodeInvalidParams {
		t.Fatalf("expected CodeInvalidParams, got %d", berr.Code)
	}
	if !strings.Contains(berr.Message, "binary") || !strings.Contains(berr.Message, "fetch_to_cloud") {
		t.Fatalf("refusal must name the file type and the way out: %q", berr.Message)
	}
}

func TestReadTextLongLineClipped(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	long := strings.Repeat("x", maxTextLineBytes+500)
	must(t, os.WriteFile(filepath.Join(root, "long.txt"), []byte(long+"\nnext\n"), 0o644))

	res, berr := tl.readText(map[string]any{"path": "long.txt"})
	if berr != nil {
		t.Fatalf("readText failed: %v", berr)
	}
	if res.Clipped != 1 {
		t.Fatalf("expected 1 clipped line, got %d", res.Clipped)
	}
	if !strings.Contains(res.Text, "[line clipped]") {
		t.Fatalf("clipped line must be marked: %q", res.Text[:80])
	}
	if !strings.Contains(res.Text, "next\n") {
		t.Fatal("the line after the clipped one must still be delivered")
	}
	// The clipped payload itself stays within the per-line cap.
	if len(strings.Split(res.Text, "\n")[0]) > maxTextLineBytes+40 {
		t.Fatalf("clipped line exceeds cap: %d", len(strings.Split(res.Text, "\n")[0]))
	}
}

func TestReadTextErrors(t *testing.T) {
	tl, root := newTestTools(t, false, 0)
	must(t, os.MkdirAll(filepath.Join(root, "dir"), 0o755))

	if _, berr := tl.readText(map[string]any{"path": "missing.txt"}); berr == nil || berr.Code != CodeNotFound {
		t.Fatalf("missing file must be CodeNotFound, got %v", berr)
	}
	if _, berr := tl.readText(map[string]any{"path": "dir"}); berr == nil || berr.Code != CodeInvalidParams {
		t.Fatalf("directory must be CodeInvalidParams, got %v", berr)
	}
}

// ---- claimFreeName ----

func TestClaimFreeName(t *testing.T) {
	root := t.TempDir()
	p := filepath.Join(root, "report.pdf")

	// The name is taken -> the first numbered sibling is claimed, atomically.
	must(t, os.WriteFile(p, []byte("v0"), 0o644))
	part1 := filepath.Join(root, "tmp-part-1")
	must(t, os.WriteFile(part1, []byte("new-bytes"), 0o644))
	got, err := claimFreeName(part1, p)
	must(t, err)
	if want := filepath.Join(root, "report (1).pdf"); got != want {
		t.Fatalf("want %q, got %q", want, got)
	}
	if data, _ := os.ReadFile(got); string(data) != "new-bytes" {
		t.Fatalf("claimed file has wrong content: %q", data)
	}
	if _, err := os.Stat(part1); !os.IsNotExist(err) {
		t.Fatalf("temp alias must be gone after the claim: %q", part1)
	}
	must(t, os.WriteFile(got, []byte("v1"), 0o644))
	part2 := filepath.Join(root, "tmp-part-2")
	must(t, os.WriteFile(part2, []byte("x"), 0o644))
	got2, err := claimFreeName(part2, p)
	must(t, err)
	if want := filepath.Join(root, "report (2).pdf"); got2 != want {
		t.Fatalf("want %q, got %q", want, got2)
	}

	// Extension-less and dot-file names keep working.
	q := filepath.Join(root, "Makefile")
	must(t, os.WriteFile(q, []byte("x"), 0o644))
	part3 := filepath.Join(root, "tmp-part-3")
	must(t, os.WriteFile(part3, []byte("y"), 0o644))
	got3, err := claimFreeName(part3, q)
	must(t, err)
	if want := filepath.Join(root, "Makefile (1)"); got3 != want {
		t.Fatalf("want %q, got %q", want, got3)
	}
}

func TestClaimFreeNameFallbackAfter999(t *testing.T) {
	root := t.TempDir()
	p := filepath.Join(root, "f.txt")
	must(t, os.WriteFile(p, []byte("x"), 0o644))
	for i := 1; i < 1000; i++ {
		must(t, os.WriteFile(filepath.Join(root, sprintf("f (%d).txt", i)), []byte("x"), 0o644))
	}
	part := filepath.Join(root, "tmp-part-f")
	must(t, os.WriteFile(part, []byte("y"), 0o644))
	got, err := claimFreeName(part, p)
	must(t, err)
	// The numbered space is exhausted: a timestamped sibling must be used,
	// and it must not collide with any of the 1000 existing names.
	if strings.Contains(got, "(") {
		t.Fatalf("expected timestamped fallback, got %q", got)
	}
	if !strings.HasPrefix(got, filepath.Join(root, "f ")) || !strings.HasSuffix(got, ".txt") {
		t.Fatalf("fallback keeps stem and extension: %q", got)
	}
	if data, _ := os.ReadFile(got); string(data) != "y" {
		t.Fatalf("claimed fallback file has wrong content: %q", data)
	}
}

// ---- runDownload: temp-file and placement behaviour ----

// downloadAdapter fakes the adapter side of a transfer_download: it hands
// out the configured bytes per transfer id (or fails /content with the
// configured status) and records /complete payloads.
type downloadAdapter struct {
	t        *testing.T
	contents map[string][]byte // transfer id -> bytes served at /content
	failHTTP int               // when non-zero, /content answers this status
	mu       sync.Mutex
	completes []map[string]any
}

func (d *downloadAdapter) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Paths are /t/<id>, /t/<id>/content, /t/<id>/complete.
	segs := strings.Split(strings.TrimPrefix(r.URL.Path, "/t/"), "/")
	id := segs[0]
	suffix := ""
	if len(segs) > 1 {
		suffix = segs[1]
	}
	switch {
	case suffix == "content":
		if d.failHTTP != 0 {
			w.WriteHeader(d.failHTTP)
			return
		}
		body, ok := d.contents[id]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		start := 0
		if m := strings.TrimPrefix(r.Header.Get("Range"), "bytes="); m != r.Header.Get("Range") {
			fmt.Sscanf(m, "%d-", &start)
		}
		w.Header().Set("Content-Range", sprintf("bytes %d-%d/%d", start, len(body)-1, len(body)))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(body[start:])
	case suffix == "complete":
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			d.t.Errorf("complete: bad json: %v", err)
		}
		d.mu.Lock()
		d.completes = append(d.completes, payload)
		d.mu.Unlock()
		w.WriteHeader(http.StatusOK)
	case r.Method == http.MethodGet:
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"state":"active","received":0}`)
	default:
		w.WriteHeader(http.StatusOK)
	}
}

func (d *downloadAdapter) lastComplete() map[string]any {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.completes) == 0 {
		return nil
	}
	return d.completes[len(d.completes)-1]
}

func newDownloadBridge(t *testing.T, root, serverURL string) *Bridge {
	t.Helper()
	cfg := NewConfig()
	cfg.ServerURL = "wss://adapter.example.com/mcp/localoctop/ws"
	cfg.Token = "test-token"
	cfg.AllowedDirs = []string{root}
	cfg.AllowWrite = true
	cfg.Logger = NopLogger{}
	must(t, cfg.Validate())
	guard := newPathGuard(cfg.AllowedDirs, NopLogger{})
	audit, err := NewAuditLogger("", 0, NopLogger{}, nil)
	must(t, err)
	b := &Bridge{cfg: cfg, log: cfg.Logger, guard: guard, tools: newTools(cfg, guard, cfg.Logger),
		audit: audit, clientID: "test-client", shutdownCh: make(chan struct{})}
	_ = serverURL
	return b
}

// TestRunDownloadConcurrentSameTarget: two deliveries aiming at the same
// destination path must each land as their own intact file — "name.ext" and
// "name (1).ext" — with no interleaved or clobbered bytes.
func TestRunDownloadConcurrentSameTarget(t *testing.T) {
	root := t.TempDir()
	contentA := bytes.Repeat([]byte("A"), 100000)[:65536]
	contentB := bytes.Repeat([]byte("B"), 100000)[:65536]
	fake := &downloadAdapter{t: t, contents: map[string][]byte{"id-A": contentA, "id-B": contentB}}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)

	b := newDownloadBridge(t, root, srv.URL)
	target := filepath.Join(root, "same.txt")
	var wg sync.WaitGroup
	for _, id := range []string{"id-A", "id-B"} {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			job := transferJob{id: id, ticket: "tk", baseURL: srv.URL + "/t/" + id}
			sha := sha256.Sum256(fake.contents[id])
			b.runDownload(job, target, int64(len(fake.contents[id])), hex.EncodeToString(sha[:]), false)
		}(id)
	}
	wg.Wait()

	entries, err := os.ReadDir(root)
	must(t, err)
	var got [][]byte
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "same") {
			data, err := os.ReadFile(filepath.Join(root, e.Name()))
			must(t, err)
			got = append(got, data)
		}
		if strings.Contains(e.Name(), ".localoctop-part-") {
			t.Fatalf("temp file left behind: %s", e.Name())
		}
	}
	if len(got) != 2 {
		t.Fatalf("expected 2 landed files, got %d", len(got))
	}
	saw := map[byte]bool{}
	for _, data := range got {
		if len(data) != 65536 {
			t.Fatalf("files must be complete: got %d bytes", len(data))
		}
		saw[data[0]] = true
	}
	if !saw['A'] || !saw['B'] {
		t.Fatalf("expected one A-file and one B-file, got first bytes %v", saw)
	}
}

// TestRunDownloadFailureCleansUp: when the adapter refuses the content
// request, the delivery fails and its exclusive temp file is removed.
func TestRunDownloadFailureCleansUp(t *testing.T) {
	root := t.TempDir()
	fake := &downloadAdapter{t: t, contents: map[string][]byte{}, failHTTP: http.StatusGone}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)

	// Shrink the retry budget so the failure surfaces fast.
	orig := transferBackoff
	transferBackoff = []time.Duration{time.Millisecond}
	t.Cleanup(func() { transferBackoff = orig })

	b := newDownloadBridge(t, root, srv.URL)
	target := filepath.Join(root, "out.bin")
	job := transferJob{id: "id-fail", ticket: "tk", baseURL: srv.URL + "/t/id-fail"}
	b.runDownload(job, target, 16, "", false)

	entries, err := os.ReadDir(root)
	must(t, err)
	for _, e := range entries {
		if strings.Contains(e.Name(), ".localoctop-part-") {
			t.Fatalf("temp file left behind after failure: %s", e.Name())
		}
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("no file must appear at the destination on failure: %v", err)
	}
	c := fake.lastComplete()
	if c == nil || c["ok"] != false {
		t.Fatalf("the adapter must be told the delivery failed, got %+v", c)
	}
}
