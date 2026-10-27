package heartbeat

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/probe"
)

type fakeProber struct {
	cpus, mem int
	err       error
}

func (f fakeProber) CPUs() int                              { return f.cpus }
func (f fakeProber) MemoryMiB(context.Context) (int, error) { return f.mem, f.err }

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func newReporter(t *testing.T, url string, p probe.Prober) *Reporter {
	t.Helper()
	r, err := New(Config{
		BaseURL:        url,
		HostName:       "host-1",
		APIKey:         "admin-key",
		Version:        "test",
		Interval:       10 * time.Millisecond,
		RequestTimeout: 200 * time.Millisecond,
		MaxBackoff:     80 * time.Millisecond,
		Reservation:    probe.Reservation{VCPUs: 1, MemoryMiB: 1024},
	}, nil, p, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestSendOnceSendsContractPayload(t *testing.T) {
	var got Payload
	var gotPath, gotKey string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		gotPath, gotKey = req.URL.Path, req.Header.Get("X-API-Key")
		if err := json.NewDecoder(req.Body).Decode(&got); err != nil {
			t.Error(err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	r := newReporter(t, srv.URL, fakeProber{cpus: 8, mem: 8192})
	if err := r.SendOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if gotPath != "/v1/admin/hosts/host-1/heartbeat" || gotKey != "admin-key" {
		t.Fatalf("path=%q key=%q", gotPath, gotKey)
	}
	want := Payload{Healthy: true, VCPUs: 7, MemoryMiB: 7168, AgentVersion: "test"}
	if got != want {
		t.Fatalf("got %+v want %+v", got, want)
	}
	st := r.Status()
	if st.Sent != 1 || st.Consecutive != 0 || st.LastCapacity == nil || st.LastCapacity.VCPUs != 7 {
		t.Fatalf("status %+v", st)
	}
}

func TestProbeFailureReportsUnhealthyInsteadOfSilence(t *testing.T) {
	var got Payload
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		_ = json.NewDecoder(req.Body).Decode(&got)
	}))
	defer srv.Close()
	r := newReporter(t, srv.URL, fakeProber{cpus: 8, err: errors.New("disk on fire")})
	if err := r.SendOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got.Healthy || got.VCPUs != 0 {
		t.Fatalf("expected unhealthy without capacity, got %+v", got)
	}
	if r.Status().LastError == "" {
		t.Fatal("probe error should be visible in status")
	}
}

func TestNon2xxIsAFailureAndCounted(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"nope"}`, http.StatusUnauthorized)
	}))
	defer srv.Close()
	r := newReporter(t, srv.URL, fakeProber{cpus: 4, mem: 4096})
	for i := 1; i <= 3; i++ {
		if err := r.SendOnce(context.Background()); err == nil {
			t.Fatal("expected error")
		}
		if c := r.Status().Consecutive; c != i {
			t.Fatalf("consecutive = %d, want %d", c, i)
		}
	}
}

func TestRequestTimeoutIsEnforced(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		select {
		case <-release:
		case <-req.Context().Done():
		}
	}))
	defer srv.Close()
	defer close(release)
	r := newReporter(t, srv.URL, fakeProber{cpus: 4, mem: 4096})
	start := time.Now()
	if err := r.SendOnce(context.Background()); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected deadline exceeded, got %v", err)
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("request was not bounded by its timeout")
	}
}

func TestBackoffIsExponentialAndCapped(t *testing.T) {
	r := newReporter(t, "http://127.0.0.1:1", fakeProber{})
	r.jitter = func(d time.Duration) time.Duration { return d } // ceiling, no randomness
	cases := map[int]time.Duration{1: 10 * time.Millisecond, 2: 20 * time.Millisecond, 3: 40 * time.Millisecond, 4: 80 * time.Millisecond, 9: 80 * time.Millisecond}
	for failures, want := range cases {
		if got := r.backoff(failures); got != want {
			t.Errorf("backoff(%d) = %s, want %s", failures, got, want)
		}
	}
}

func TestRunRecoversAfterFailuresAndStopsOnCancel(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) <= 2 {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	defer srv.Close()
	r := newReporter(t, srv.URL, fakeProber{cpus: 4, mem: 4096})

	var mu sync.Mutex
	var waits []time.Duration
	ctx, cancel := context.WithCancel(context.Background())
	r.jitter = func(d time.Duration) time.Duration { return d }
	r.sleep = func(ctx context.Context, d time.Duration) error {
		mu.Lock()
		waits = append(waits, d)
		n := len(waits)
		mu.Unlock()
		if n == 4 {
			cancel()
		}
		return ctx.Err()
	}
	if err := r.Run(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("Run returned %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	want := []time.Duration{10 * time.Millisecond, 20 * time.Millisecond, 10 * time.Millisecond, 10 * time.Millisecond}
	for i := range want {
		if waits[i] != want[i] {
			t.Fatalf("waits = %v, want %v (backoff then reset to interval)", waits, want)
		}
	}
	if st := r.Status(); st.Sent != 2 || st.Consecutive != 0 {
		t.Fatalf("status %+v", st)
	}
}

func TestConfigValidation(t *testing.T) {
	bad := []Config{
		{BaseURL: "http://x", Interval: time.Second, RequestTimeout: time.Second, MaxBackoff: time.Second},                // no host
		{HostName: "h", BaseURL: "ftp://x", Interval: time.Second, RequestTimeout: time.Second, MaxBackoff: time.Second},  // scheme
		{HostName: "h", BaseURL: "http://x", Interval: 0, RequestTimeout: time.Second, MaxBackoff: time.Second},           // interval
		{HostName: "h", BaseURL: "http://x", Interval: time.Minute, RequestTimeout: time.Second, MaxBackoff: time.Second}, // backoff < interval
	}
	for i, c := range bad {
		if _, err := New(c, nil, fakeProber{}, quietLogger()); err == nil {
			t.Errorf("config %d should be rejected", i)
		}
	}
}

func TestStatusIsSafeForConcurrentUse(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()
	r := newReporter(t, srv.URL, fakeProber{cpus: 4, mem: 4096})
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func() { defer wg.Done(); _ = r.SendOnce(context.Background()) }()
		go func() { defer wg.Done(); _ = r.Status() }()
	}
	wg.Wait()
	if r.Status().Sent != 8 {
		t.Fatalf("sent = %d", r.Status().Sent)
	}
}
