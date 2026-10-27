#!/usr/bin/env bash
# ONLINE side: build images from pinned inputs and export everything the offline side needs.
#
#   deploy/airgap/build-bundle.sh [output-dir]       (default: dist/airgap-bundle)
#
# Produces:
#   images/*.tar.gz        docker save output, one per image
#   compose.yml            offline compose file (internal network, pull_policy: never)
#   smoke.py, install-bundle.sh, verify-offline.sh, .env.example
#   MANIFEST.txt           provenance: git commit, build time, image IDs, base image digests
#   SHA256SUMS             checksums of every file above
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/dist/airgap-bundle}"
API_IMAGE=vm-lifecycle-manager:airgap
AGENT_IMAGE=vmlm-hostagent:airgap
PLATFORM="${PLATFORM:-}"   # e.g. linux/arm64 for an Apple Silicon target; empty = native

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }  # macOS ships shasum
rm -rf "$OUT" && mkdir -p "$OUT/images"
platform_flag=()
[ -n "$PLATFORM" ] && platform_flag=(--platform "$PLATFORM")

echo ">> building images (this is the only step that needs network access)"
docker build "${platform_flag[@]}" -t "$API_IMAGE" "$ROOT"
docker build "${platform_flag[@]}" -t "$AGENT_IMAGE" "$ROOT/agent"

echo ">> exporting images"
docker save "$API_IMAGE" | gzip > "$OUT/images/vm-lifecycle-manager.tar.gz"
docker save "$AGENT_IMAGE" | gzip > "$OUT/images/vmlm-hostagent.tar.gz"

cp "$ROOT/deploy/airgap/compose.offline.yml" "$OUT/compose.yml"
cp "$ROOT/deploy/airgap/install-bundle.sh" "$ROOT/deploy/airgap/verify-offline.sh" "$OUT/"
cp "$ROOT/scripts/smoke.py" "$ROOT/.env.example" "$OUT/"

base_digest() { docker image inspect --format '{{index .RepoDigests 0}}' "$1" 2>/dev/null || echo "unknown"; }
{
  echo "bundle-created: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git-commit:     $(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo 'not a git checkout')"
  echo "git-dirty:      $(git -C "$ROOT" status --porcelain 2>/dev/null | grep -q . && echo yes || echo no)"
  echo "platform:       ${PLATFORM:-native ($(docker version --format '{{.Server.Os}}/{{.Server.Arch}}'))}"
  echo "requirements.lock sha256: $(sha256 "$ROOT/requirements.lock" | cut -d' ' -f1)"
  for img in "$API_IMAGE" "$AGENT_IMAGE"; do
    echo "image $img id: $(docker image inspect --format '{{.Id}}' "$img")"
  done
  echo "base python:3.13-slim-bookworm: $(base_digest python:3.13-slim-bookworm)"
  echo "base golang:1.22-bookworm:      $(base_digest golang:1.22-bookworm)"
  echo "base distroless/static:nonroot: $(base_digest gcr.io/distroless/static-debian12:nonroot)"
} > "$OUT/MANIFEST.txt"

export -f sha256
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 bash -c 'sha256 "$@"' _ > SHA256SUMS)
echo ">> bundle ready: $OUT"
cat "$OUT/MANIFEST.txt"
