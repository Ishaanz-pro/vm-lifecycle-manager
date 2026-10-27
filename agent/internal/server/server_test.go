package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ishaanz-pro/vm-lifecycle-manager/agent/internal/heartbeat"
)

type staticStatus heartbeat.Status

func (s staticStatus) Status() heartbeat.Status { return heartbeat.Status(s) }

func get(t *testing.T, h http.Handler, path string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	return rec.Code, body
}

func TestEndpoints(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }

	never := Handler(staticStatus{}, time.Minute, "v1", clock)
	if code, body := get(t, never, "/healthz"); code != 200 || body["version"] != "v1" {
		t.Fatalf("healthz %d %v", code, body)
	}
	if code, _ := get(t, never, "/readyz"); code != 503 {
		t.Fatalf("readyz before any heartbeat = %d, want 503", code)
	}

	fresh := Handler(staticStatus{LastSuccess: now.Add(-30 * time.Second), Sent: 3}, time.Minute, "v1", clock)
	if code, _ := get(t, fresh, "/readyz"); code != 200 {
		t.Fatalf("readyz fresh = %d", code)
	}
	if _, body := get(t, fresh, "/v1/status"); body["sent"] != float64(3) {
		t.Fatalf("status %v", body)
	}

	stale := Handler(staticStatus{LastSuccess: now.Add(-2 * time.Minute)}, time.Minute, "v1", clock)
	if code, _ := get(t, stale, "/readyz"); code != 503 {
		t.Fatalf("readyz stale = %d", code)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := Handler(staticStatus{}, time.Minute, "v1", time.Now)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/healthz", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /healthz = %d", rec.Code)
	}
}
