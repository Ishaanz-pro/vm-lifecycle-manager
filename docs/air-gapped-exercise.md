# Air-gapped deployment exercise

An educational exercise inspired by disconnected environments. It does **not** reproduce
Google Distributed Cloud air-gapped or any vendor product; it shows the core discipline:
everything the runtime needs is built, pinned, checksummed and carried across, and the
runtime fetches nothing.

> **Verification status:** the scripts are syntax-checked and the smoke test
> (`scripts/smoke.py`) has been run against a live server, including its negative check
> (on an internet-connected machine `--expect-no-egress` correctly fails). The full
> bundle → load → offline-run flow requires Docker and **has not been executed** on the
> author's machine (no Docker installed). Run it yourself before claiming it works.

## 1. Pin

* Python runtime dependencies: `requirements.lock` (exact versions, generated from a clean
  venv; `pyproject.toml` keeps compatible ranges for development).
* Go agent: standard library only (`go.mod` has no requirements).
* Base images: referenced by tag in the Dockerfiles; `MANIFEST.txt` records their resolved
  digests at build time. For stronger pinning, replace tags with `@sha256:` digests.

## 2. Build and export (online machine)

```bash
deploy/airgap/build-bundle.sh                         # native arch
PLATFORM=linux/arm64 deploy/airgap/build-bundle.sh    # e.g. built on x86 for an M1 target
```

Output, `dist/airgap-bundle/`: image tarballs, offline compose file, install/verify scripts,
smoke test, `MANIFEST.txt` (git commit, dirty flag, image IDs, base-image digests, lock hash)
and `SHA256SUMS`.

## 3. Transfer

Copy the directory by whatever medium the exercise allows (USB disk, `scp` to a second
machine, a shared folder into a VM). Verify the copy arrived intact on the far side (step 4
does this before anything else).

## 4. Install and run (offline machine)

```bash
cd airgap-bundle
./install-bundle.sh      # sha256 --check, docker load, generate .env, compose up --wait
./verify-offline.sh      # prove no egress, then health + full lifecycle smoke test
```

## What makes this a genuine offline test

| Control | Effect |
|---|---|
| `networks.isolated.internal: true` | Docker gives the network no route outside |
| `pull_policy: never`, no `build:` | a missing image is an error, never a download |
| no published ports | only containers on the isolated network can reach the API |
| `smoke.py --expect-no-egress` | **fails** the run if any public address is reachable |

A *stronger* version: run step 4 on a second machine or VM with networking disabled at the
OS level (e.g. a UTM VM with its network device removed). The Docker-internal network
proves the containers are isolated; a disconnected machine also proves Docker itself fetched
nothing.

This is *not* the same as a normal deployment with a restrictive firewall: there the
runtime could still reach a package mirror or registry, and missing artifacts would be
fetched silently.

## Limits

* Image provenance is recorded, not signed (no cosign/SLSA attestations).
* No vulnerability scan is part of the bundle; scanning must happen on the online side.
* Updates mean shipping a whole new bundle; there is no delta mechanism.
