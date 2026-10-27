// Package server exposes the agent's local health and status endpoints.
//
//	GET /healthz    200 while the process is serving (liveness)
//	GET /readyz     200 once at least one heartbeat succeeded recently, else 503 (readiness)
//	GET /v1/status  JSON heartbeat status for debugging
package server

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/heartbeat"
)

// StatusSource is satisfied by *heartbeat.Reporter.
type StatusSource interface {
	Status() heartbeat.Status
}

// Handler builds the HTTP handler. now is injectable for tests.
func Handler(src StatusSource, staleAfter time.Duration, version string, now func() time.Time) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "version": version})
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		st := src.Status()
		fresh := !st.LastSuccess.IsZero() && now().Sub(st.LastSuccess) <= staleAfter
		code, status := http.StatusOK, "ready"
		if !fresh {
			code, status = http.StatusServiceUnavailable, "not_ready"
		}
		writeJSON(w, code, map[string]any{"status": status, "last_success": st.LastSuccess})
	})
	mux.HandleFunc("GET /v1/status", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, src.Status())
	})
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
