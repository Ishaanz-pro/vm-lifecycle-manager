"""OS-level behaviour: safe subprocesses, timeouts, signals, pids, paths, ports."""

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from vmlm import sysinfo

PY = sys.executable


def test_run_command_captures_exit_code_and_output() -> None:
    res = sysinfo.run_command(
        [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        timeout_s=10,
    )
    assert res.exit_code == 3 and not res.ok and not res.timed_out
    assert res.stdout.strip() == "out" and res.stderr.strip() == "err"


def test_run_command_never_uses_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    # if this went through a shell, `;` would run a second command creating the marker
    res = sysinfo.run_command(
        [PY, "-c", "import sys; print(sys.argv[1])", f"x; touch {marker}"], timeout_s=10
    )
    assert res.ok and res.stdout.strip() == f"x; touch {marker}"
    assert not marker.exists()


def test_run_command_timeout_kills_the_child() -> None:
    res = sysinfo.run_command([PY, "-c", "import time; time.sleep(30)"], timeout_s=0.5, grace_s=0.5)
    assert res.timed_out and res.exit_code is None and res.duration_s < 10


@pytest.mark.skipif(sysinfo.IS_WINDOWS, reason="process groups / signals are POSIX")
def test_timeout_also_kills_grandchildren(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time;"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    sysinfo.run_command([PY, "-c", script], timeout_s=1, grace_s=0.5)
    grandchild = int(pidfile.read_text())
    assert sysinfo.wait_for_exit(grandchild, 5), "grandchild survived: orphaned process"


@pytest.mark.skipif(sysinfo.IS_WINDOWS, reason="POSIX signals")
def test_signal_termination_is_reported() -> None:
    res = sysinfo.run_command(
        [PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"], timeout_s=10
    )
    assert res.exit_code == -15 and res.signal == "SIGTERM"


def test_disallowed_and_missing_executables_rejected() -> None:
    with pytest.raises(sysinfo.CommandNotAllowed):
        sysinfo.run_command([PY, "-c", "pass"], timeout_s=5, allowed=frozenset({"qemu-img"}))
    with pytest.raises(FileNotFoundError):
        sysinfo.run_command(["definitely-not-a-real-binary-xyz"], timeout_s=5)
    with pytest.raises(sysinfo.CommandNotAllowed):
        sysinfo.run_command([], timeout_s=5)


def test_output_is_truncated() -> None:
    res = sysinfo.run_command(
        [PY, "-c", f"print('x' * {sysinfo.MAX_OUTPUT_BYTES * 2})"], timeout_s=10
    )
    assert res.stdout.endswith("[truncated]") and len(res.stdout) < sysinfo.MAX_OUTPUT_BYTES + 100


def test_pid_alive_and_pidfile(tmp_path: Path) -> None:
    assert sysinfo.pid_alive(os.getpid())
    assert not sysinfo.pid_alive(0) and not sysinfo.pid_alive(-1)
    proc = subprocess.Popen([PY, "-c", "pass"])
    proc.wait()
    assert not sysinfo.pid_alive(proc.pid)
    pidfile = tmp_path / "x.pid"
    assert sysinfo.read_pidfile(pidfile) is None
    pidfile.write_text("garbage")
    assert sysinfo.read_pidfile(pidfile) is None
    pidfile.write_text(f"{os.getpid()}\n")
    assert sysinfo.read_pidfile(pidfile) == os.getpid()


def test_resources_are_reported() -> None:
    assert sysinfo.cpu_count() >= 1
    mem = sysinfo.memory_total_mib()
    assert mem is None or mem > 0


def test_inspect_path_and_private_dir(tmp_path: Path) -> None:
    missing = sysinfo.inspect_path(tmp_path / "nope")
    assert not missing.exists and not missing.writable
    d = sysinfo.ensure_private_dir(tmp_path / "state")
    report = sysinfo.inspect_path(d)
    assert report.exists and report.is_dir and report.writable
    if not sysinfo.IS_WINDOWS:
        assert report.mode == "0o700" and not report.world_writable


def test_port_checks() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    assert sysinfo.check_tcp_port("127.0.0.1", port).open
    server.close()
    free = sysinfo.find_free_port()
    closed = sysinfo.check_tcp_port("127.0.0.1", free, timeout_s=0.5)
    assert not closed.open and closed.error
    with pytest.raises(ValueError):
        sysinfo.check_tcp_port("127.0.0.1", 70000)


def test_virtualization_report_and_doctor() -> None:
    caps = sysinfo.virtualization_capabilities()
    assert caps.accelerators["tcg"] is True
    assert set(caps.qemu) == {"qemu-system-aarch64", "qemu-system-x86_64", "qemu-img"}
    report = sysinfo.doctor()
    assert report["cpu_count"] >= 1 and report["paths"][0]["exists"]
