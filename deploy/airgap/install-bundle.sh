#!/usr/bin/env bash
# OFFLINE side: verify the bundle, load images, start the stack. Never pulls or downloads.
#
#   cd airgap-bundle && ./install-bundle.sh
set -euo pipefail
cd "$(dirname "$0")"

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }  # macOS ships shasum
echo ">> verifying checksums"
sha256 --check --strict SHA256SUMS   # any tampered or truncated file aborts here

echo ">> loading images"
for tarball in images/*.tar.gz; do
  gunzip -c "$tarball" | docker load
done

if [ ! -f .env ]; then
  admin="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))' 2>/dev/null || head -c 24 /dev/urandom | base64)"
  printf 'VMLM_ADMIN_API_KEY=%s\nVMLM_API_KEY=%s\n' "$admin" "$admin-user" > .env
  chmod 600 .env
  echo ">> generated .env with random keys (mode 600)"
fi

echo ">> starting stack (pull_policy: never)"
docker compose -f compose.yml up -d --wait
docker compose -f compose.yml ps
