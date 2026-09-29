//go:build windows

package localoctop

import (
	"os"
	"path/filepath"
	"testing"
)

// TestTrashPath_WindowsRecycleBin proves the SHFileOperationW call actually
// moves a file to the recycle bin on a real Windows machine (the employee
// target). Skips if the shell refuses (e.g. a CI runner with recycling
// disabled) — the refusal path is what removeOne turns into an error, so a
// skip here is honest, not a silent pass.
func TestTrashPath_WindowsRecycleBin(t *testing.T) {
	root := t.TempDir()
	f := filepath.Join(root, "trashme.txt")
	if err := os.WriteFile(f, []byte("recycle me"), 0o644); err != nil {
		t.Fatal(err)
	}
	moved, err := trashPath(f)
	if err != nil {
		t.Fatalf("trashPath returned a hard error: %v", err)
	}
	if !moved {
		// Shell declined to recycle here (recycle bin off / drive policy).
		t.Skipf("shell offers no recycle bin on this machine/volume; refusing path verified separately")
	}
	if _, statErr := os.Stat(f); !os.IsNotExist(statErr) {
		t.Fatalf("file still present after recycle: %v", statErr)
	}
}
