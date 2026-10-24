#!/usr/bin/env bash
# Demo: create -> capacity -> stop -> start -> delete against a running server.
# Usage: scripts/demo.sh [base_url]     (default http://127.0.0.1:8000)
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
AUTH=()
[ -n "${VMLM_API_KEY:-}" ] && AUTH=(-H "X-API-Key: $VMLM_API_KEY")

PY="${PYTHON:-python3}"
pp() { "$PY" -m json.tool; }
call() { curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' "$@"; }

echo "== create"
RESP=$(call -X POST "$BASE/v1/instances" -H "Idempotency-Key: demo-$(date +%s)" \
  -d '{"name":"demo-vm","vcpus":2,"memory_mib":4096}')
echo "$RESP" | pp
ID=$(echo "$RESP" | "$PY" -c'import sys,json; print(json.load(sys.stdin)["id"])')

echo "== capacity (2 vCPU / 4096 MiB reserved)"
call "$BASE/v1/hosts/capacity" | pp

echo "== stop";   call -X POST "$BASE/v1/instances/$ID/stop"  | pp
echo "== start";  call -X POST "$BASE/v1/instances/$ID/start" | pp
echo "== delete (terminate)"; call -X DELETE "$BASE/v1/instances/$ID" | pp

echo "== capacity (released)"
call "$BASE/v1/hosts/capacity" | pp

echo "== audit events"
call "$BASE/v1/instances/$ID/events" | pp
