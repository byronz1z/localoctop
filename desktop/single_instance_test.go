//go:build windows

package main

// Tests for the T11 single-instance decision logic. The four-row table from
// the rework brief is covered by decideSecondDesktopAction's unit tests
// (probe result is injected), plus one live integration test that takes the
// real mutex to prove contention is detected.

import (
	"testing"
	"time"

	"github.com/byronz1z/localoctop"
)

// TestDecideSecondDesktopAction_FourCases covers the brief's table:
//
//	running  | second launch | expected action
//	desktop  | desktop       | bringToFront + exit (Wails will raise the window)
//	desktop  | background    | stderrMsg + exit (no console for a window raise)
//	background| desktop      | messageBox + exit (no window to raise, need a visible reason)
//	background| background   | stderrMsg + exit (the original SECOND_INSTANCE_CHECK path)
func TestDecideSecondDesktopAction_FourCases(t *testing.T) {
	cases := []struct {
		name        string
		running     localoctop.ProbeResult
		secondIsGUI bool
		want        secondDesktopAction
	}{
		{"desktop_then_desktop", localoctop.ProbeDesktop, true, actBringToFront},
		{"desktop_then_background", localoctop.ProbeDesktop, false, actStderrExit},
		{"background_then_desktop", localoctop.ProbeBackground, true, actMessageBox},
		{"background_then_background", localoctop.ProbeBackground, false, actStderrExit},
		// Defensive: ProbeNone should never reach decide (the caller would
		// have taken the lock instead), but if it does, refuse to start.
		{"none_then_desktop", localoctop.ProbeNone, true, actMessageBox},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := decideSecondDesktopAction(tc.running, tc.secondIsGUI)
			if got != tc.want {
				t.Fatalf("decideSecondDesktopAction(%v, gui=%v) = %v, want %v",
					tc.running, tc.secondIsGUI, got, tc.want)
			}
		})
	}
}

// TestSecondDesktopAction_Strings makes sure the action enum stays printable
// (used by any future log lines) and distinct.
func TestSecondDesktopAction_Strings(t *testing.T) {
	seen := map[string]bool{}
	for _, a := range []secondDesktopAction{actBringToFront, actMessageBox, actStderrExit} {
		s := a.String()
		if seen[s] {
			t.Fatalf("duplicate action string %q", s)
		}
		seen[s] = true
	}
}

// TestAcquireSingleInstance_SecondLaunchFails holds the real mutex and
// verifies the shared-lock acquisition fails, and that the probe classifies
// the holder as background (no Wails window exists in this test process).
func TestAcquireSingleInstance_SecondLaunchFails(t *testing.T) {
	release, ok := localoctop.AcquireSingleInstance()
	if !ok {
		t.Fatal("first acquisition must succeed")
	}
	defer release()

	if _, ok2 := localoctop.AcquireSingleInstance(); ok2 {
		t.Fatal("second acquisition while the first holds the lock must fail")
	}

	probe := localoctop.ProbeSingleInstance()
	if probe == localoctop.ProbeNone {
		t.Fatal("probe must detect the held lock")
	}
	if probe != localoctop.ProbeBackground {
		t.Fatalf("probe = %v, want ProbeBackground (no Wails window in a test binary)", probe)
	}

	// After release, acquisition works again.
	release()
	time.Sleep(50 * time.Millisecond)
	release2, ok3 := localoctop.AcquireSingleInstance()
	if !ok3 {
		t.Fatal("acquisition after release must succeed")
	}
	release2()
}
