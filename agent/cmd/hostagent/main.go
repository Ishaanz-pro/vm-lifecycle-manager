// Command hostagent runs on a compute host, measures its capacity, and heartbeats it to the
// VM Lifecycle Manager control plane. It also serves local /healthz, /readyz and /v1/status.
//
// Configuration comes from flags, each with an environment-variable default. The admin key
// is read only from the environment (VMLM_AGENT_API_KEY) so it never appears in `ps` output.
//
// Shutdown: SIGINT/SIGTERM cancel the root context. The heartbeat loop stops at its next
// wait, the HTTP server drains in-flight requests for up to -shutdown-timeout, and the
// process exits 0. A second signal is not needed and forces nothing.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/heartbeat"
	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/probe"
	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/server"
)

var version = "0.1.0" // overridden at build time: -ldflags "-X main.version=..."

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "hostagent:", err)
		os.Exit(1)
	}
}

func run() error {
	fs := flag.NewFlagSet("hostagent", flag.ContinueOnError)
	hostName := fs.String("host", env("VMLM_AGENT_HOST_NAME", ""), "host name registered in the control plane")
	baseURL := fs.String("control-plane", env("VMLM_AGENT_CONTROL_PLANE_URL", "http://127.0.0.1:8000"), "control plane base URL")
	listen := fs.String("listen", env("VMLM_AGENT_LISTEN", "127.0.0.1:9100"), "address for health/status endpoints")
	interval := fs.Duration("interval", envDuration("VMLM_AGENT_INTERVAL", 10*time.Second), "heartbeat interval")
	timeout := fs.Duration("request-timeout", envDuration("VMLM_AGENT_REQUEST_TIMEOUT", 3*time.Second), "per-request timeout")
	maxBackoff := fs.Duration("max-backoff", envDuration("VMLM_AGENT_MAX_BACKOFF", time.Minute), "maximum retry backoff")
	reserveCPU := fs.Int("reserve-vcpus", envInt("VMLM_AGENT_RESERVE_VCPUS", 1), "vCPUs held back for the host")
	reserveMem := fs.Int("reserve-memory-mib", envInt("VMLM_AGENT_RESERVE_MEMORY_MIB", 1024), "MiB held back for the host")
	shutdownTimeout := fs.Duration("shutdown-timeout", 5*time.Second, "graceful shutdown deadline")
	if err := fs.Parse(os.Args[1:]); err != nil {
		return err
	}

	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	reporter, err := heartbeat.New(heartbeat.Config{
		BaseURL:        *baseURL,
		HostName:       *hostName,
		APIKey:         os.Getenv("VMLM_AGENT_API_KEY"),
		Version:        version,
		Interval:       *interval,
		RequestTimeout: *timeout,
		MaxBackoff:     *maxBackoff,
		Reservation:    probe.Reservation{VCPUs: *reserveCPU, MemoryMiB: *reserveMem},
	}, nil, probe.System{}, log)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	srv := &http.Server{
		Addr:              *listen,
		Handler:           server.Handler(reporter, 3**interval, version, time.Now),
		ReadHeaderTimeout: 5 * time.Second,
	}
	errCh := make(chan error, 1)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		log.Info("agent_listening", "addr", *listen)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- fmt.Errorf("http server: %w", err)
			stop()
		}
	}()
	go func() {
		defer wg.Done()
		log.Info("agent_started", "host", *hostName, "control_plane", *baseURL, "version", version)
		_ = reporter.Run(ctx) // returns when ctx is cancelled
	}()

	<-ctx.Done()
	log.Info("agent_shutting_down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), *shutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("http_shutdown_failed", "error", err.Error())
	}
	wg.Wait()
	select {
	case err := <-errCh:
		return err
	default:
		log.Info("agent_stopped")
		return nil
	}
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if n, err := strconv.Atoi(os.Getenv(key)); err == nil {
		return n
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if d, err := time.ParseDuration(os.Getenv(key)); err == nil {
		return d
	}
	return def
}
