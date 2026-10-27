"""Operating-system inspection and safe process control.

Used by the QEMU provider and by `python -m vmlm.sysinfo` (a read-only "doctor" report).
Rules this module enforces:
  * subprocesses are started from an argv list with shell=False; nothing is ever passed
    through a shell, and executables are resolved to absolute paths first
  * every child has a timeout; on timeout the whole process group is terminated (SIGTERM,
    then SIGKILL after a grace period) so no orphan keeps running
  * output is truncated so a chatty child cannot exhaust memory
Platform differences (Linux / macOS / Windows) are detected at runtime, never assumed.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 64 * 1024
IS_WINDOWS = sys.platform == "win32"


class CommandNotAllowed(ValueError):
    pass


# ---- subprocess ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None  # None when the child was killed after a timeout
    signal: str | None  # e.g. "SIGKILL" when the child died from a signal (POSIX)
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def resolve_executable(name: str) -> str | None:
    """Absolute path of an executable, or None. Accepts a bare name or an absolute path."""
    if os.path.isabs(name):
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    return shutil.which(name)


def run_command(
    argv: list[str],
    *,
    timeout_s: float,
    allowed: frozenset[str] | None = None,
    grace_s: float = 2.0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run argv[0] with arguments, never via a shell, bounded by timeout_s.

    `allowed` restricts which executable basenames may run (defence in depth for callers
    that build argv from configuration).
    """
    if not argv or not all(isinstance(a, str) for a in argv):
        raise CommandNotAllowed("argv must be a non-empty list of strings")
    exe = resolve_executable(argv[0])
    if exe is None:
        raise FileNotFoundError(f"executable not found: {argv[0]}")
    if allowed is not None and Path(exe).name not in allowed:
        raise CommandNotAllowed(f"{Path(exe).name} is not in the allowed set")

    popen_kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True  # own process group, killable as a unit

    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False, resolved executable
        [exe, *argv[1:]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=env,
        **popen_kwargs,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc, grace_s)
        out, err = proc.communicate()
    code = proc.returncode
    sig = None
    if code is not None and code < 0 and not IS_WINDOWS:
        sig = signal.Signals(-code).name
    return CommandResult(
        argv=tuple(argv),
        exit_code=None if timed_out else code,
        signal=sig,
        stdout=_truncate(out),
        stderr=_truncate(err),
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
    )


def terminate_process_tree(proc: subprocess.Popen[bytes], grace_s: float) -> None:
    """SIGTERM the child's process group, wait `grace_s`, then SIGKILL. Never leaves orphans
    from the group behind. On Windows, terminate() maps to TerminateProcess."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        proc.terminate()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    if IS_WINDOWS:
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    proc.wait()


def _truncate(data: bytes | None) -> str:
    if not data:
        return ""
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return text + ("\n...[truncated]" if len(data) > MAX_OUTPUT_BYTES else "")


# ---- processes -------------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Whether a process with this PID exists (it may belong to another user)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)  # signal 0: existence/permission check only, nothing is delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pidfile(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None
    return int(text) if text.isdigit() else None


def signal_pid(pid: int, sig: signal.Signals) -> bool:
    """Send a signal; returns False if the process is already gone."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def wait_for_exit(pid: int, timeout_s: float, poll_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(poll_s)
    return not pid_alive(pid)


# ---- resources ----------------------------------------------------------------------------------


def cpu_count() -> int:
    """CPUs this process may use (respects Linux affinity/cgroup cpusets where exposed)."""
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def memory_total_mib() -> int | None:
    system = platform.system()
    if system == "Linux":
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
        except OSError:
            return None
    if system == "Darwin":
        res = run_command(
            ["sysctl", "-n", "hw.memsize"], timeout_s=2, allowed=frozenset({"sysctl"})
        )
        return int(res.stdout.strip()) // (1024 * 1024) if res.ok else None
    if system == "Windows":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return int(status.ullTotalPhys // (1024 * 1024))
    return None


def load_average() -> tuple[float, float, float] | None:
    return os.getloadavg() if hasattr(os, "getloadavg") else None


# ---- filesystem -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PathReport:
    path: str
    exists: bool
    is_dir: bool
    mode: str | None
    readable: bool
    writable: bool
    owner_uid: int | None
    world_writable: bool


def inspect_path(path: str | Path) -> PathReport:
    p = Path(path)
    if not p.exists():
        return PathReport(str(p), False, False, None, False, False, None, False)
    st = p.stat()
    return PathReport(
        path=str(p.resolve()),
        exists=True,
        is_dir=p.is_dir(),
        mode=oct(stat.S_IMODE(st.st_mode)),
        readable=os.access(p, os.R_OK),
        writable=os.access(p, os.W_OK),
        owner_uid=None if IS_WINDOWS else st.st_uid,
        world_writable=bool(st.st_mode & stat.S_IWOTH) and not IS_WINDOWS,
    )


def ensure_private_dir(path: str | Path) -> Path:
    """Create a directory readable only by the current user (0700 on POSIX)."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        p.chmod(0o700)
    return p


# ---- network ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PortCheck:
    host: str
    port: int
    open: bool
    latency_ms: float | None
    error: str | None


def check_tcp_port(host: str, port: int, timeout_s: float = 1.0) -> PortCheck:
    if not 0 < port < 65536:
        raise ValueError("port out of range")
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return PortCheck(host, port, True, (time.monotonic() - started) * 1000, None)
    except OSError as exc:
        return PortCheck(host, port, False, None, f"{type(exc).__name__}: {exc}")


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the kernel for an unused port. Inherently racy (TOCTOU) until the caller binds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


# ---- virtualization ---------------------------------------------------------------------------


@dataclass
class VirtualizationReport:
    system: str
    machine: str
    accelerators: dict[str, bool] = field(default_factory=dict)
    qemu: dict[str, str | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def virtualization_capabilities(
    qemu_binaries: tuple[str, ...] = ("qemu-system-aarch64", "qemu-system-x86_64", "qemu-img"),
) -> VirtualizationReport:
    system, machine = platform.system(), platform.machine()
    report = VirtualizationReport(system=system, machine=machine)
    if system == "Linux":
        kvm = Path("/dev/kvm")
        report.accelerators["kvm"] = kvm.exists() and os.access(kvm, os.R_OK | os.W_OK)
        if kvm.exists() and not report.accelerators["kvm"]:
            report.notes.append(
                "/dev/kvm exists but is not read/writable: add user to the kvm group"
            )
    elif system == "Darwin":
        res = run_command(
            ["sysctl", "-n", "kern.hv_support"], timeout_s=2, allowed=frozenset({"sysctl"})
        )
        report.accelerators["hvf"] = res.ok and res.stdout.strip() == "1"
        report.notes.append("KVM does not exist on macOS; QEMU uses Hypervisor.framework (hvf)")
    elif system == "Windows":
        report.accelerators["whpx"] = False
        report.notes.append("Windows is not a supported host for the QEMU provider")
    report.accelerators["tcg"] = True  # QEMU software emulation; slow but always available
    for name in qemu_binaries:
        report.qemu[name] = resolve_executable(name)
    return report


# ---- doctor -------------------------------------------------------------------------------------


def doctor(paths: tuple[str, ...] = (".",)) -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "cpu_count": cpu_count(),
        "memory_total_mib": memory_total_mib(),
        "load_average": load_average(),
        "paths": [asdict(inspect_path(p)) for p in paths],
        "virtualization": asdict(virtualization_capabilities()),
    }


if __name__ == "__main__":
    print(json.dumps(doctor(tuple(sys.argv[1:]) or (".",)), indent=2))
