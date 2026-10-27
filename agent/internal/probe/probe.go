// Package probe measures the host the agent runs on.
//
// Capacity reported to the control plane is the measured capacity minus a configurable
// reservation for the host OS and the hypervisor itself, so the scheduler never plans to
// hand out memory the host needs to stay alive.
package probe

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Capacity is what the host can offer to instances.
type Capacity struct {
	VCPUs     int `json:"vcpus"`
	MemoryMiB int `json:"memory_mib"`
}

// Reservation is held back for the host itself.
type Reservation struct {
	VCPUs     int
	MemoryMiB int
}

// ErrUnsupported is returned on platforms where memory cannot be measured.
var ErrUnsupported = errors.New("memory probe not supported on this platform")

// Prober measures raw host resources. It is an interface so tests can substitute values.
type Prober interface {
	CPUs() int
	MemoryMiB(ctx context.Context) (int, error)
}

// System probes the real machine.
type System struct{}

// CPUs returns the number of logical CPUs usable by this process.
func (System) CPUs() int { return runtime.NumCPU() }

// MemoryMiB returns total physical memory in MiB.
func (System) MemoryMiB(ctx context.Context) (int, error) {
	switch runtime.GOOS {
	case "linux":
		f, err := os.Open("/proc/meminfo")
		if err != nil {
			return 0, fmt.Errorf("open /proc/meminfo: %w", err)
		}
		defer f.Close()
		return ParseMeminfo(f)
	case "darwin":
		ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		// Fixed argv, no shell, bounded by the context deadline.
		out, err := exec.CommandContext(ctx, "sysctl", "-n", "hw.memsize").Output()
		if err != nil {
			return 0, fmt.Errorf("sysctl hw.memsize: %w", err)
		}
		bytes, err := strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
		if err != nil {
			return 0, fmt.Errorf("parse hw.memsize %q: %w", out, err)
		}
		return int(bytes / (1024 * 1024)), nil
	default:
		return platformMemoryMiB() // probe_windows.go / probe_other.go
	}
}

// ParseMeminfo extracts MemTotal (kB) from /proc/meminfo content and returns MiB.
func ParseMeminfo(r io.Reader) (int, error) {
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) >= 2 && fields[0] == "MemTotal:" {
			kb, err := strconv.Atoi(fields[1])
			if err != nil {
				return 0, fmt.Errorf("parse MemTotal %q: %w", fields[1], err)
			}
			return kb / 1024, nil
		}
	}
	if err := sc.Err(); err != nil {
		return 0, err
	}
	return 0, errors.New("MemTotal not found")
}

// Available returns measured capacity minus the reservation. It is an error for the
// reservation to consume the whole host: such a host must report unhealthy, not zero.
func Available(ctx context.Context, p Prober, res Reservation) (Capacity, error) {
	mem, err := p.MemoryMiB(ctx)
	if err != nil {
		return Capacity{}, err
	}
	c := Capacity{VCPUs: p.CPUs() - res.VCPUs, MemoryMiB: mem - res.MemoryMiB}
	if c.VCPUs <= 0 || c.MemoryMiB <= 0 {
		return Capacity{}, fmt.Errorf("reservation %+v leaves no capacity (cpus=%d mem=%dMiB)", res, p.CPUs(), mem)
	}
	return c, nil
}
