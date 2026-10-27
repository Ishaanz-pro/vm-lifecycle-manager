# Virtualization: simulated vs real

## Simulated provider (default)

`VMLM_PROVIDER=simulated`. No hypervisor, no VMs, works everywhere including an M1 MacBook
Air with nothing installed. `GET /health` reports `"provider": {"simulated": true,
"real_vms": false}`. It models: create/start/stop/delete/inspect, idempotent keyed calls,
injected permanent/transient failures, injected latency (timeouts), and external drift.
Its state is process memory, so a control-plane restart forgets the "VMs" (tests keep the
provider object across a restart to stand in for a hypervisor that outlives the control
plane).

## QEMU provider (opt-in, capability-gated)

`VMLM_PROVIDER=qemu`. Runs each instance as a real QEMU process.

> **Verification status:** the provider's capability gating, argv construction, QMP
> protocol handling, state derivation and path safety are unit-tested without QEMU. A real
> boot test exists (`tests/test_qemu_provider.py::test_real_qemu_lifecycle`) but runs only
> with `VMLM_QEMU_E2E=1` and **has not been run** by the author of this document, whose
> development machine was Windows (unsupported host). Treat real-VM support as
> experimental until you have run it on your own machine.

### Supported hosts

| Host | Accelerator | Notes |
|---|---|---|
| macOS on Apple Silicon | `hvf` (Hypervisor.framework) | **KVM does not exist on macOS.** Use `qemu-system-aarch64` and an arm64 guest image. |
| Linux with `/dev/kvm` readable+writable | `kvm` | add your user to the `kvm` group |
| any (incl. inside a VM without nested virt) | `tcg` | software emulation; correct but slow |
| Windows | — | not supported; the provider refuses to start |

### Setup on an M1 Mac

```bash
brew install qemu                      # provides qemu-system-aarch64, qemu-img, edk2 firmware
python -m vmlm.sysinfo                 # check: virtualization.accelerators.hvf == true
# Any arm64 cloud image in qcow2 format works as a read-only base, e.g. Debian/Ubuntu "genericcloud" arm64.
export VMLM_PROVIDER=qemu
export VMLM_QEMU_BASE_IMAGE=$HOME/images/debian-12-genericcloud-arm64.qcow2
export VMLM_QEMU_FIRMWARE=$(brew --prefix qemu)/share/qemu/edk2-aarch64-code.fd
export VMLM_QEMU_ACCEL=hvf             # default
VMLM_QEMU_E2E=1 pytest tests/test_qemu_provider.py -k real -rs
uvicorn vmlm.main:create_app --factory
```

If any prerequisite is missing, startup fails with a message listing **every** problem
(missing binary, image, firmware, unavailable accelerator). It never falls back to the
simulator.

### How it works

| Operation | Implementation |
|---|---|
| provision | `qemu-img create -f qcow2 -b <base> -F qcow2 disk.qcow2` into a temp dir, renamed into place atomically; then start |
| start | `qemu-system-* ... -daemonize -pidfile qemu.pid -qmp tcp:127.0.0.1:<free port>` (argv list, no shell) |
| stop | QMP `system_powerdown` (ACPI) → wait → QMP `quit` → `SIGTERM` → `SIGKILL`, each bounded |
| inspect | no directory → missing; live pid in pidfile → RUNNING; otherwise STOPPED |
| delete | stop, then remove the VM directory |

Because `inspect` is derived from files and the process table, it is correct after a
control-plane restart, which the reconciler relies on.

Safety: provider IDs are matched against `^vm-<uuid>$` so they cannot escape the state
directory; the state directory is created `0700`; only `qemu-system-*`/`qemu-img` may be
executed; QMP listens on loopback only; guests get no network (`-nic none`).

Limitations: no guest networking, no cloud-init/SSH, no console attach, CPU/memory are
fixed at create time, one QMP connection per call.
