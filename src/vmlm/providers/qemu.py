"""Capability-gated QEMU provider: runs real local VMs as QEMU processes.

Selected only with VMLM_PROVIDER=qemu. `from_settings` checks every prerequisite (binaries,
accelerator, base image, firmware, state directory) and raises ProviderUnavailable with a
specific message if anything is missing. It never falls back to simulation.

Supported hosts (see docs/virtualization.md):
  * macOS on Apple Silicon: qemu-system-aarch64 -accel hvf (Hypervisor.framework); no KVM
  * Linux with /dev/kvm: -accel kvm
  * anywhere: -accel tcg (software emulation; functional but slow)
Windows is not supported.

Per-VM layout under qemu_state_dir/<provider_id>/ (directory mode 0700):
  disk.qcow2    copy-on-write overlay backed by the read-only base image
  meta.json     vcpus, memory_mib, qmp port
  qemu.pid      written by QEMU (-pidfile)
  qmp.port      QMP control socket port (127.0.0.1 only)

State model: inspect() derives state from the filesystem and the process table, so it is
correct after a control-plane restart (unlike the simulated provider):
  no directory                     -> None (VM does not exist)
  directory, pid alive             -> RUNNING
  directory, no live pid           -> STOPPED

Guest networking is not configured (`-nic none`); this provider demonstrates lifecycle and
process control, not a usable cloud VM.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vmlm import sysinfo
from vmlm.config import Settings
from vmlm.providers.base import ProviderError, ProviderUnavailable, ProviderVMState

log = logging.getLogger("vmlm.providers.qemu")

PROVIDER_ID_RE = re.compile(r"^vm-[0-9a-f-]{36}$")  # never lets an id escape the state dir
ALLOWED_BINARIES = frozenset({"qemu-system-aarch64", "qemu-system-x86_64", "qemu-img"})


@dataclass(frozen=True)
class QemuConfig:
    qemu_binary: str  # absolute path
    qemu_img_binary: str  # absolute path
    base_image: Path
    state_dir: Path
    accel: str
    machine: str
    firmware: Path | None
    shutdown_timeout_s: float


def build_qemu_argv(
    cfg: QemuConfig, vm_dir: Path, vcpus: int, memory_mib: int, qmp_port: int
) -> list[str]:
    """Pure function: the exact argv used to boot a VM. Unit-tested; no shell involved."""
    argv = [
        cfg.qemu_binary,
        "-name", vm_dir.name,
        "-machine", cfg.machine,
        "-accel", cfg.accel,
        "-cpu", "host" if cfg.accel in {"hvf", "kvm"} else "max",
        "-smp", str(vcpus),
        "-m", f"{memory_mib}M",
        "-drive", f"file={vm_dir / 'disk.qcow2'},if=virtio,format=qcow2",
        "-nic", "none",
        "-display", "none",
        "-serial", f"file:{vm_dir / 'console.log'}",
        "-monitor", "none",
        "-qmp", f"tcp:127.0.0.1:{qmp_port},server=on,wait=off",
        "-pidfile", str(vm_dir / "qemu.pid"),
        "-daemonize",
    ]  # fmt: skip
    if cfg.firmware is not None:
        argv += ["-bios", str(cfg.firmware)]
    return argv


class QmpClient:
    """Minimal QEMU Machine Protocol client (JSON over TCP), with a deadline on every call."""

    def __init__(self, port: int, timeout_s: float = 5.0, host: str = "127.0.0.1") -> None:
        self.host, self.port, self.timeout_s = host, port, timeout_s

    def execute(self, command: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            stream = sock.makefile("rwb")
            greeting = self._read(stream)
            if "QMP" not in greeting:
                raise ProviderError("unexpected QMP greeting", retryable=True)
            self._send(stream, {"execute": "qmp_capabilities"})
            self._read_return(stream)
            payload: dict[str, Any] = {"execute": command}
            if arguments:
                payload["arguments"] = arguments
            self._send(stream, payload)
            return self._read_return(stream)

    @staticmethod
    def _send(stream: Any, obj: dict[str, Any]) -> None:
        stream.write(json.dumps(obj).encode() + b"\n")
        stream.flush()

    @staticmethod
    def _read(stream: Any) -> dict[str, Any]:
        line = stream.readline()
        if not line:
            raise ProviderError("QMP connection closed", retryable=True)
        return json.loads(line)  # type: ignore[no-any-return]

    def _read_return(self, stream: Any) -> dict[str, Any]:
        while True:  # skip asynchronous events until our reply arrives
            msg = self._read(stream)
            if "return" in msg:
                return msg["return"]  # type: ignore[no-any-return]
            if "error" in msg:
                raise ProviderError(f"QMP error: {msg['error'].get('desc', msg['error'])}")


class QemuProvider:
    def __init__(self, cfg: QemuConfig) -> None:
        self.cfg = cfg

    # ---- construction / capability gating ---------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings) -> QemuProvider:
        problems: list[str] = []
        if sysinfo.IS_WINDOWS:
            raise ProviderUnavailable("the QEMU provider does not support Windows hosts")
        qemu = sysinfo.resolve_executable(settings.qemu_binary)
        qemu_img = sysinfo.resolve_executable(settings.qemu_img_binary)
        if qemu is None:
            problems.append(f"{settings.qemu_binary} not found (macOS: brew install qemu)")
        elif Path(qemu).name not in ALLOWED_BINARIES:
            problems.append(f"{Path(qemu).name} is not an allowed QEMU binary")
        if qemu_img is None:
            problems.append(f"{settings.qemu_img_binary} not found")
        if not settings.qemu_base_image:
            problems.append("VMLM_QEMU_BASE_IMAGE is not set (path to a qcow2 base image)")
        elif not Path(settings.qemu_base_image).is_file():
            problems.append(f"base image {settings.qemu_base_image} does not exist")
        if settings.qemu_firmware and not Path(settings.qemu_firmware).is_file():
            problems.append(f"firmware {settings.qemu_firmware} does not exist")
        caps = sysinfo.virtualization_capabilities()
        if not caps.accelerators.get(settings.qemu_accel, False):
            problems.append(
                f"accelerator {settings.qemu_accel!r} is unavailable on "
                f"{caps.system}/{caps.machine}"
                f" (available: {sorted(k for k, v in caps.accelerators.items() if v)})"
            )
        if problems:
            raise ProviderUnavailable("QEMU provider unavailable: " + "; ".join(problems))
        state_dir = sysinfo.ensure_private_dir(settings.qemu_state_dir)
        assert qemu is not None and qemu_img is not None
        return cls(
            QemuConfig(
                qemu_binary=qemu,
                qemu_img_binary=qemu_img,
                base_image=Path(settings.qemu_base_image).resolve(),  # type: ignore[arg-type]
                state_dir=state_dir.resolve(),
                accel=settings.qemu_accel,
                machine=settings.qemu_machine,
                firmware=Path(settings.qemu_firmware).resolve() if settings.qemu_firmware else None,
                shutdown_timeout_s=settings.qemu_shutdown_timeout_s,
            )
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": "qemu",
            "simulated": False,
            "real_vms": True,
            "accel": self.cfg.accel,
            "machine": self.cfg.machine,
        }

    # ---- VMProvider -------------------------------------------------------------------------

    def provision(self, provider_id: str, vcpus: int, memory_mib: int) -> None:
        vm_dir = self._dir(provider_id)
        if not (vm_dir / "disk.qcow2").exists():
            tmp = vm_dir.with_name(vm_dir.name + ".creating")
            shutil.rmtree(tmp, ignore_errors=True)
            sysinfo.ensure_private_dir(tmp)
            self._run(
                [
                    self.cfg.qemu_img_binary,
                    "create",
                    "-f",
                    "qcow2",
                    "-b",
                    str(self.cfg.base_image),
                    "-F",
                    "qcow2",
                    str(tmp / "disk.qcow2"),
                ],  # fmt: skip
                timeout_s=60,
                what="create overlay",
            )
            (tmp / "meta.json").write_text(json.dumps({"vcpus": vcpus, "memory_mib": memory_mib}))
            tmp.rename(vm_dir)  # atomic publish: a half-created VM is never visible
        self.start(provider_id)

    def start(self, provider_id: str) -> None:
        vm_dir = self._require(provider_id)
        if self._pid(vm_dir) is not None:
            return  # already running: idempotent
        meta = json.loads((vm_dir / "meta.json").read_text())
        port = sysinfo.find_free_port()
        (vm_dir / "qmp.port").write_text(str(port))
        argv = build_qemu_argv(self.cfg, vm_dir, meta["vcpus"], meta["memory_mib"], port)
        self._run(argv, timeout_s=30, what="boot")  # -daemonize: returns once the VM is up
        if self._pid(vm_dir) is None:
            raise ProviderError("QEMU exited immediately; see console.log", retryable=False)

    def stop(self, provider_id: str) -> None:
        vm_dir = self._require(provider_id)
        pid = self._pid(vm_dir)
        if pid is None:
            return  # already stopped: idempotent
        try:  # 1. ask the guest to shut down (ACPI power button)
            self._qmp(vm_dir).execute("system_powerdown")
        except (OSError, ProviderError) as exc:
            log.warning(
                "qmp_powerdown_failed", extra={"provider_id": provider_id, "error": str(exc)}
            )
        if sysinfo.wait_for_exit(pid, self.cfg.shutdown_timeout_s):
            return
        log.warning("guest_shutdown_timeout", extra={"provider_id": provider_id})
        try:  # 2. stop QEMU itself
            self._qmp(vm_dir).execute("quit")
        except (OSError, ProviderError):
            sysinfo.signal_pid(pid, signal.SIGTERM)
        if sysinfo.wait_for_exit(pid, 5):
            return
        sysinfo.signal_pid(pid, signal.SIGKILL)  # 3. last resort
        if not sysinfo.wait_for_exit(pid, 5):
            raise ProviderError(f"QEMU pid {pid} did not exit after SIGKILL", retryable=True)

    def inspect(self, provider_id: str) -> ProviderVMState | None:
        vm_dir = self._dir(provider_id)
        if not (vm_dir / "disk.qcow2").exists():
            return None
        return ProviderVMState.RUNNING if self._pid(vm_dir) is not None else ProviderVMState.STOPPED

    def delete(self, provider_id: str) -> None:
        vm_dir = self._dir(provider_id)
        if not vm_dir.exists():
            return
        self.stop(provider_id)
        shutil.rmtree(vm_dir)

    # ---- internals --------------------------------------------------------------------------

    def _dir(self, provider_id: str) -> Path:
        if not PROVIDER_ID_RE.match(provider_id):
            raise ProviderError(f"invalid provider id {provider_id!r}")
        return self.cfg.state_dir / provider_id

    def _require(self, provider_id: str) -> Path:
        vm_dir = self._dir(provider_id)
        if not (vm_dir / "disk.qcow2").exists():
            raise ProviderError(f"no such VM {provider_id!r}")
        return vm_dir

    @staticmethod
    def _pid(vm_dir: Path) -> int | None:
        pid = sysinfo.read_pidfile(vm_dir / "qemu.pid")
        return pid if pid is not None and sysinfo.pid_alive(pid) else None

    @staticmethod
    def _qmp(vm_dir: Path) -> QmpClient:
        return QmpClient(int((vm_dir / "qmp.port").read_text()))

    @staticmethod
    def _run(argv: list[str], *, timeout_s: float, what: str) -> None:
        started = time.monotonic()
        try:
            res = sysinfo.run_command(argv, timeout_s=timeout_s, allowed=ALLOWED_BINARIES)
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise ProviderError(f"{what}: {exc}", retryable=False) from exc
        if res.timed_out:
            raise ProviderError(f"{what} timed out after {timeout_s}s", retryable=True)
        if not res.ok:
            detail = res.stderr.strip().splitlines()[-1:] or [f"exit {res.exit_code}"]
            raise ProviderError(f"{what} failed: {detail[0]}", retryable=False)
        log.info("qemu_command_ok", extra={"what": what, "seconds": time.monotonic() - started})
