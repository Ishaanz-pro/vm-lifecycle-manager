#!/usr/bin/env bash
# OFFLINE side: prove the network is disconnected, then run the lifecycle smoke test,
# from a container attached to the same internal-only network as the services.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a

docker compose -f compose.yml run --rm --no-deps \
  -v "$PWD/smoke.py:/smoke/smoke.py:ro" \
  --entrypoint python smoke /smoke/smoke.py \
  --base-url http://api:8000 --api-key "$VMLM_ADMIN_API_KEY" --expect-no-egress
