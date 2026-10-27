// Package heartbeat periodically reports host health and capacity to the control plane.
//
// Wire contract (control plane API v1):
//
//	POST {base}/v1/admin/hosts/{name}/heartbeat
//	X-API-Key: <admin key>            (omitted when no key is configured)
//	{"healthy": bool, "vcpus": int?, "memory_mib": int?, "agent_version": string}
//
// A 2xx response is success. Anything else, or a transport error, is a failure; the
// reporter backs off exponentially with full jitter, capped at MaxBackoff, and resets
// to the normal interval after the next success. The control plane marks the host
// UNHEALTHY on its own if heartbeats stop arriving, so the agent never needs to be
// reachable from the control plane.
package heartbeat

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/probe"
)

// Payload is the JSON body sent to the control plane.
type Payload struct {
	Healthy      bool   `json:"healthy"`
	VCPUs        int    `json:"vcpus,omitempty"`
	MemoryMiB    int    `json:"memory_mib,omitempty"`
	AgentVersion string `json:"agent_version"`
}

// Status is the reporter's view of its own progress, served on /v1/status.
type Status struct {
	LastAttempt  time.Time       `json:"last_attempt"`
	LastSuccess  time.Time       `json:"last_success"`
	LastError    string          `json:"last_error,omitempty"`
	Consecutive  int             `json:"consecutive_failures"`
	LastCapacity *probe.Capacity `json:"last_capacity,omitempty"`
	Sent         int             `json:"sent"`
}

// Config controls a Reporter.
type Config struct {
	BaseURL        string
	HostName       string
	APIKey         string // never logged
	Version        string
	Interval       time.Duration
	RequestTimeout time.Duration
	MaxBackoff     time.Duration
	Reservation    probe.Reservation
}

// Reporter sends heartbeats until its context is cancelled.
type Reporter struct {
	cfg    Config
	client *http.Client
	prober probe.Prober
	log    *slog.Logger
	sleep  func(context.Context, time.Duration) error // injectable for tests
	jitter func(time.Duration) time.Duration

	mu     sync.Mutex
	status Status
}

// New builds a Reporter. client may be nil (a client with no global timeout is used; each
// request is bounded by cfg.RequestTimeout through its context).
func New(cfg Config, client *http.Client, prober probe.Prober, log *slog.Logger) (*Reporter, error) {
	if cfg.HostName == "" {
		return nil, fmt.Errorf("host name is required")
	}
	u, err := url.Parse(cfg.BaseURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("invalid control plane URL %q", cfg.BaseURL)
	}
	if cfg.Interval <= 0 || cfg.RequestTimeout <= 0 || cfg.MaxBackoff < cfg.Interval {
		return nil, fmt.Errorf("invalid timing config: interval=%s timeout=%s maxBackoff=%s",
			cfg.Interval, cfg.RequestTimeout, cfg.MaxBackoff)
	}
	if client == nil {
		client = &http.Client{}
	}
	return &Reporter{
		cfg:    cfg,
		client: client,
		prober: prober,
		log:    log,
		sleep:  sleepCtx,
		jitter: func(d time.Duration) time.Duration { return time.Duration(rand.Int64N(int64(d) + 1)) },
	}, nil
}

// Status returns a copy of the current status. Safe for concurrent use.
func (r *Reporter) Status() Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.status
}

// Run sends heartbeats until ctx is done. It returns ctx.Err() on cancellation.
func (r *Reporter) Run(ctx context.Context) error {
	for {
		err := r.SendOnce(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		wait := r.cfg.Interval
		if err != nil {
			wait = r.backoff(r.Status().Consecutive)
			r.log.Warn("heartbeat_failed", "error", err.Error(), "retry_in", wait.String())
		}
		if err := r.sleep(ctx, wait); err != nil {
			return err
		}
	}
}

// SendOnce probes the host and sends one heartbeat.
func (r *Reporter) SendOnce(ctx context.Context) error {
	payload := Payload{Healthy: true, AgentVersion: r.cfg.Version}
	capacity, probeErr := probe.Available(ctx, r.prober, r.cfg.Reservation)
	if probeErr != nil {
		payload.Healthy = false // still report: "alive but unfit" beats silence
	} else {
		payload.VCPUs, payload.MemoryMiB = capacity.VCPUs, capacity.MemoryMiB
	}
	err := r.post(ctx, payload)
	r.mu.Lock()
	defer r.mu.Unlock()
	r.status.LastAttempt = time.Now().UTC()
	if err != nil {
		r.status.Consecutive++
		r.status.LastError = err.Error()
		return err
	}
	r.status.Consecutive = 0
	r.status.LastError = ""
	r.status.LastSuccess = r.status.LastAttempt
	r.status.Sent++
	if probeErr == nil {
		r.status.LastCapacity = &capacity
	} else {
		r.status.LastError = "probe: " + probeErr.Error()
	}
	return nil
}

func (r *Reporter) post(ctx context.Context, p Payload) error {
	body, err := json.Marshal(p)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, r.cfg.RequestTimeout)
	defer cancel()
	endpoint := fmt.Sprintf("%s/v1/admin/hosts/%s/heartbeat", r.cfg.BaseURL, url.PathEscape(r.cfg.HostName))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if r.cfg.APIKey != "" {
		req.Header.Set("X-API-Key", r.cfg.APIKey)
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return fmt.Errorf("post heartbeat: %w", err)
	}
	defer resp.Body.Close()
	snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("control plane returned %d: %s", resp.StatusCode, bytes.TrimSpace(snippet))
	}
	return nil
}

// backoff returns a full-jitter delay in [0, min(MaxBackoff, Interval*2^(failures-1))].
func (r *Reporter) backoff(failures int) time.Duration {
	ceiling := r.cfg.Interval
	for i := 1; i < failures && ceiling < r.cfg.MaxBackoff; i++ {
		ceiling *= 2
	}
	if ceiling > r.cfg.MaxBackoff {
		ceiling = r.cfg.MaxBackoff
	}
	return r.jitter(ceiling)
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
