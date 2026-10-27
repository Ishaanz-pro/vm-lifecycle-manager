package probe

import (
	"context"
	"errors"
	"strings"
	"testing"
)

type fake struct {
	cpus int
	mem  int
	err  error
}

func (f fake) CPUs() int                              { return f.cpus }
func (f fake) MemoryMiB(context.Context) (int, error) { return f.mem, f.err }

func TestParseMeminfo(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    int
		wantErr bool
	}{
		{"typical", "MemTotal:       16314280 kB\nMemFree:  1000 kB\n", 15931, false},
		{"not first line", "Foo: 1 kB\nMemTotal: 2048 kB\n", 2, false},
		{"missing", "MemFree: 10 kB\n", 0, true},
		{"garbage", "MemTotal: lots kB\n", 0, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseMeminfo(strings.NewReader(tc.input))
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, tc.wantErr)
			}
			if got != tc.want {
				t.Fatalf("got %d, want %d", got, tc.want)
			}
		})
	}
}

func TestAvailableSubtractsReservation(t *testing.T) {
	got, err := Available(context.Background(), fake{cpus: 8, mem: 16384}, Reservation{VCPUs: 1, MemoryMiB: 2048})
	if err != nil {
		t.Fatal(err)
	}
	if got != (Capacity{VCPUs: 7, MemoryMiB: 14336}) {
		t.Fatalf("got %+v", got)
	}
}

func TestAvailableRejectsReservationThatConsumesHost(t *testing.T) {
	if _, err := Available(context.Background(), fake{cpus: 2, mem: 1024}, Reservation{VCPUs: 2}); err == nil {
		t.Fatal("expected error when reservation leaves no vCPUs")
	}
	if _, err := Available(context.Background(), fake{cpus: 2, mem: 1024}, Reservation{MemoryMiB: 4096}); err == nil {
		t.Fatal("expected error when reservation leaves no memory")
	}
}

func TestAvailablePropagatesProbeError(t *testing.T) {
	boom := errors.New("boom")
	if _, err := Available(context.Background(), fake{cpus: 2, err: boom}, Reservation{}); !errors.Is(err, boom) {
		t.Fatalf("got %v", err)
	}
}

func TestSystemProbeReturnsSomethingSane(t *testing.T) {
	if n := (System{}).CPUs(); n < 1 {
		t.Fatalf("cpus = %d", n)
	}
	mem, err := (System{}).MemoryMiB(context.Background())
	if errors.Is(err, ErrUnsupported) {
		t.Skip("memory probe unsupported on this OS")
	}
	if err != nil || mem <= 0 {
		t.Fatalf("mem=%d err=%v", mem, err)
	}
}
