package console

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/byronz1z/localoctop/internal/appcfg"
)

// withPicker installs a fake native dialog for the duration of a test so no
// real window is ever spawned (headless discipline).
func withPicker(t *testing.T, p picker) {
	t.Helper()
	SetPicker(p)
	t.Cleanup(func() { SetPicker(nil) })
}

func TestBrowseReturnsPickedPath(t *testing.T) {
	withPicker(t, func(ctx context.Context) (string, bool, error) {
		return `C:\Users\me\Documents\Octop`, true, nil
	})
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Get(base + "/api/browse")
	if err != nil {
		t.Fatalf("GET browse: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var out struct {
		Selected bool   `json:"selected"`
		Path     string `json:"path"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !out.Selected || out.Path != `C:\Users\me\Documents\Octop` {
		t.Errorf("browse = %+v", out)
	}
}

func TestBrowseCancelNoChange(t *testing.T) {
	withPicker(t, func(ctx context.Context) (string, bool, error) {
		return "", false, nil // user cancelled
	})
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Get(base + "/api/browse")
	if err != nil {
		t.Fatalf("GET browse: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("cancel must be 200, got %d", resp.StatusCode)
	}
	var out struct {
		Selected bool   `json:"selected"`
		Path     string `json:"path"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out.Selected || out.Path != "" {
		t.Errorf("cancel must yield selected=false path empty, got %+v", out)
	}
}

func TestBrowsePickerErrorSurfaces(t *testing.T) {
	withPicker(t, func(ctx context.Context) (string, bool, error) {
		return "", false, errors.New("picker failed: WinForms unavailable")
	})
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Get(base + "/api/browse")
	if err != nil {
		t.Fatalf("GET browse: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", resp.StatusCode)
	}
	var out map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out["error"] == "" {
		t.Error("picker error must be surfaced")
	}
}

func TestBrowseSerialLockSecondBusy(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{})
	var once sync.Once
	withPicker(t, func(ctx context.Context) (string, bool, error) {
		once.Do(func() { close(started) })
		<-release // hold the dialog open until the test releases it
		return `C:\x`, true, nil
	})
	_, base := startTestServer(t, appcfg.Default(), nil)

	// First request holds the lock.
	first := make(chan *http.Response, 1)
	go func() {
		resp, err := http.Get(base + "/api/browse")
		if err != nil {
			t.Errorf("first GET: %v", err)
			first <- nil
			return
		}
		first <- resp
	}()
	<-started // wait until the picker is actually holding the lock

	// Second request must be rejected, not queued into a second window.
	resp, err := http.Get(base + "/api/browse")
	if err != nil {
		t.Fatalf("second GET: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Errorf("second concurrent browse = %d, want 409", resp.StatusCode)
	}

	close(release)
	r := <-first
	if r != nil {
		r.Body.Close()
		if r.StatusCode != http.StatusOK {
			t.Errorf("first browse = %d, want 200", r.StatusCode)
		}
	}
}

func TestBrowseMethodNotAllowed(t *testing.T) {
	withPicker(t, func(ctx context.Context) (string, bool, error) { return "", false, nil })
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Post(base+"/api/browse", "application/json", nil)
	if err != nil {
		t.Fatalf("POST browse: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("POST browse = %d, want 405", resp.StatusCode)
	}
}

func TestCommonDirsReturnsExistingOnly(t *testing.T) {
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Get(base + "/api/commondirs")
	if err != nil {
		t.Fatalf("GET commondirs: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var out struct {
		Dirs []CommonDir `json:"dirs"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	// The user's home directory always exists, so at least one entry is
	// guaranteed; every returned entry must exist and be a directory.
	if len(out.Dirs) == 0 {
		t.Fatal("commondirs returned nothing; at least the home dir must be listed")
	}
	for _, d := range out.Dirs {
		st, err := os.Stat(d.Path)
		if err != nil || !st.IsDir() {
			t.Errorf("commondir %q does not exist or is not a dir", d.Path)
		}
		if d.Name == "" {
			t.Errorf("commondir %q has empty name", d.Path)
		}
	}
}

func TestCommonDirsMethodNotAllowed(t *testing.T) {
	_, base := startTestServer(t, appcfg.Default(), nil)
	resp, err := http.Post(base+"/api/commondirs", "application/json", nil)
	if err != nil {
		t.Fatalf("POST commondirs: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("POST commondirs = %d, want 405", resp.StatusCode)
	}
}

func TestConfigPostRemembersServer(t *testing.T) {
	var applied appcfg.File
	srv, base := startTestServer(t, appcfg.Default(), func(c appcfg.File) error {
		applied = c
		return nil
	})

	body := `{"server_url":"wss://a.example/ws","token":"t1","allowed_dirs":[{"path":"C:/tmp","enabled":true}]}`
	resp, err := http.Post(base+"/api/config", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	resp.Body.Close()
	if len(applied.RecentServers) != 1 || applied.RecentServers[0].URL != "wss://a.example/ws" || applied.RecentServers[0].Token != "t1" {
		t.Fatalf("recent after first save = %+v", applied.RecentServers)
	}
	srv.SetConfig(applied)

	// Second connection: both remembered, newest first.
	body = `{"server_url":"wss://b.example/ws","token":"t2","allowed_dirs":[{"path":"C:/tmp","enabled":true}]}`
	resp, err = http.Post(base+"/api/config", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	resp.Body.Close()
	srv.SetConfig(applied)

	resp, err = http.Get(base + "/api/config")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	var view configView
	if err := json.NewDecoder(resp.Body).Decode(&view); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(view.RecentServers) != 2 {
		t.Fatalf("recent = %+v, want 2 entries", view.RecentServers)
	}
	if view.RecentServers[0].URL != "wss://b.example/ws" || view.RecentServers[0].Token != "t2" {
		t.Errorf("front = %+v, want newest first", view.RecentServers[0])
	}
}
