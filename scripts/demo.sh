#!/usr/bin/env bash
# Demo: create -> capacity -> stop -> start -> terminate -> audit trail, against a running
# server. Mutations are asynchronous (202 + operation); `?wait=10` asks the server to wait.
#   scripts/demo.sh [base_url]          (default http://127.0.0.1:8000)
#   VMLM_API_KEY=... scripts/demo.sh    when authentication is enabled
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
PY="${PYTHON:-python3}"
AUTH=()
[ -n "${VMLM_API_KEY:-}" ] && AUTH=(-H "X-API-Key: $VMLM_API_KEY")

pp() { "$PY" -m json.tool; }
call() { curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' "$@"; }
field() { "$PY" -c "import sys,json; print(json.load(sys.stdin)$1)"; }

echo "== health (note: provider.simulated = true)"; call "$BASE/health" | pp

echo "== create (async: returns an operation)"
OP=$(call -X POST "$BASE/v1/instances" -H "Idempotency-Key: demo-$(date +%s)" \
  -d '{"name":"demo-vm","vcpus":2,"memory_mib":4096}')
echo "$OP" | pp
OP_ID=$(echo "$OP" | field "['id']"); ID=$(echo "$OP" | field "['instance_id']")

echo "== poll the operation until it finishes"
for _ in $(seq 1 50); do
  STATUS=$(call "$BASE/v1/operations/$OP_ID" | field "['status']")
  [ "$STATUS" = SUCCEEDED ] || [ "$STATUS" = FAILED ] && break
  sleep 0.1
done
echo "operation $OP_ID: $STATUS"

echo "== capacity (2 vCPU / 4096 MiB now reserved)"; call "$BASE/v1/hosts/capacity" | pp
echo "== stop";   call -X POST "$BASE/v1/instances/$ID/stop?wait=10"  | field "['status']"
echo "== start";  call -X POST "$BASE/v1/instances/$ID/start?wait=10" | field "['status']"
echo "== terminate"; call -X DELETE "$BASE/v1/instances/$ID?wait=10" | field "['status']"
echo "== instance record is kept as TERMINATED"; call "$BASE/v1/instances/$ID" | field "['state']"
echo "== capacity (released)"; call "$BASE/v1/hosts/capacity" | field "['reserved_vcpus']"
echo "== audit trail"
call "$BASE/v1/instances/$ID/events?limit=100" \
  | "$PY" -c "import sys,json; [print(f\"{e['id']:>3} {e['event_type']:<26} {e['from_state'] or '':<13} {e['to_state'] or ''}\") for e in json.load(sys.stdin)['items']]"
