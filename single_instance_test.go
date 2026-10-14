package localoctop

import (
	"testing"
	"time"
)

func TestAcquireSingleInstance_FirstSucceeds(t *testing.T) {
	release, ok := AcquireSingleInstance()
	if !ok {
		t.Fatal("first acquisition must succeed")
	}
	if release == nil {
		t.Fatal("release function must not be nil")
	}
	release()
}

func TestAcquireSingleInstance_SecondFails(t *testing.T) {
	release1, ok1 := AcquireSingleInstance()
	if !ok1 {
		t.Fatal("first acquisition must succeed")
	}
	defer release1()

	_, ok2 := AcquireSingleInstance()
	if ok2 {
		t.Fatal("second acquisition while the first holds the lock must fail")
	}
}

func TestAcquireSingleInstance_ReleaseThenReacquire(t *testing.T) {
	release1, ok1 := AcquireSingleInstance()
	if !ok1 {
		t.Fatal("first acquisition must succeed")
	}
	release1()

	// Give the OS a moment to drop the handle/lock.
	time.Sleep(50 * time.Millisecond)

	release2, ok2 := AcquireSingleInstance()
	if !ok2 {
		t.Fatal("acquisition after release must succeed")
	}
	release2()
}
