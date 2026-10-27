"""QEMU provider tests that need no QEMU: capability gating, argv construction, QMP protocol,
state derivation, path safety. A real boot test runs only with VMLM_QEMU_E2E=1."""

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from vmlm import sysinfo
from vmlm.config import Settings
from vmlm.main import create_app
from vmlm.providers.base import ProviderError, ProviderUnavailable, ProviderVMState
from vmlm.providers.qemu import QemuConfig, QemuProvider, QmpClient, build_qemu_argv

VM_ID = "vm-12345678-1234-1234-1234-123456789abc"


def cfg(tmp_path: Path, accel: str = "hvf", firmware: Path | None = None) -> QemuConfig:
    return QemuConfig(
        qemu_binary="/opt/homebrew/bin/qemu-system-aarch64",
        qemu_img_binary="/opt/homebrew/bin/qemu-img",
        base_image=tmp_path / "base.qcow2",
        state_dir=tmp_path,
        accel=accel,
        machine="virt",
        firmware=firmware,
        shutdown_timeout_s=1,
    )


def test_unavailable_prerequisites_fail_loudly_never_fall_back(tmp_path: Path) -> None:
    settings = Settings(
        provider="qemu",
        qemu_binary="no-such-qemu-binary",
        qemu_img_binary="no-such-qemu-img",
        qemu_base_image=str(tmp_path / "missing.qcow2"),
        qemu_state_dir=str(tmp_path / "state"),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        QemuProvider.from_settings(settings)
    msg = str(exc.value)
    if not sysinfo.IS_WINDOWS:
        assert "no-such-qemu-binary not found" in msg and "missing.qcow2 does not exist" in msg
    with pytest.raises(ProviderUnavailable):  # the app refuses to start, rather than simulating
        create_app(settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'x.db'}"}))


def test_argv_is_a_list_with_expected_flags(tmp_path: Path) -> None:
    vm_dir = tmp_path / VM_ID
    argv = build_qemu_argv(cfg(tmp_path), vm_dir, 2, 1024, 47001)
    assert argv[0].endswith("qemu-system-aarch64")
    pairs = dict(zip(argv[1::2], argv[2::2], strict=False))
    assert pairs["-accel"] == "hvf" and pairs["-cpu"] == "host"
    assert pairs["-smp"] == "2" and pairs["-m"] == "1024M"
    assert pairs["-qmp"] == "tcp:127.0.0.1:47001,server=on,wait=off"  # loopback only
    assert pairs["-nic"] == "none" and "-daemonize" in argv
    assert "-bios" not in argv
    tcg = build_qemu_argv(cfg(tmp_path, "tcg", tmp_path / "fw.fd"), vm_dir, 1, 512, 1)
    assert dict(zip(tcg[1::2], tcg[2::2], strict=False))["-cpu"] == "max"
    assert tcg[-2:] == ["-bios", str(tmp_path / "fw.fd")]


def test_provider_ids_cannot_escape_state_dir(tmp_path: Path) -> None:
    p = QemuProvider(cfg(tmp_path))
    for bad in ("../../etc", "vm-../x", "/tmp/x", "vm-1"):
        with pytest.raises(ProviderError):
            p.inspect(bad)


def test_state_is_derived_from_disk_and_process_table(tmp_path: Path) -> None:
    p = QemuProvider(cfg(tmp_path))
    assert p.inspect(VM_ID) is None
    vm_dir = tmp_path / VM_ID
    vm_dir.mkdir()
    (vm_dir / "disk.qcow2").write_bytes(b"")
    assert p.inspect(VM_ID) is ProviderVMState.STOPPED
    (vm_dir / "qemu.pid").write_text(str(os.getpid()))  # a live pid stands in for QEMU
    assert p.inspect(VM_ID) is ProviderVMState.RUNNING
    (vm_dir / "qemu.pid").write_text("999999")
    assert p.inspect(VM_ID) is ProviderVMState.STOPPED  # stale pidfile is not "running"
    p.stop(VM_ID)  # not running: idempotent no-op
    p.delete(VM_ID)
    assert not vm_dir.exists() and p.inspect(VM_ID) is None
    p.delete(VM_ID)  # deleting a missing VM is not an error
    with pytest.raises(ProviderError):
        p.start(VM_ID)


class FakeQmpServer:
    """Speaks just enough QMP: greeting, capabilities negotiation, one command, an event."""

    def __init__(self, reply: dict[str, object]) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.reply = reply
        self.received: list[dict[str, object]] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        f = conn.makefile("rwb")

        def send(o: dict[str, object]) -> None:
            f.write(json.dumps(o).encode() + b"\n")
            f.flush()

        send({"QMP": {"version": {}, "capabilities": []}})
        for _ in range(2):
            msg = json.loads(f.readline())
            self.received.append(msg)
            if msg["execute"] == "qmp_capabilities":
                send({"return": {}})
            else:
                send({"event": "POWERDOWN", "timestamp": {}})  # async event before the reply
                send(self.reply)
        conn.close()


def test_qmp_client_negotiates_and_skips_events() -> None:
    server = FakeQmpServer({"return": {"status": "running"}})
    assert QmpClient(server.port).execute("query-status") == {"status": "running"}
    assert [m["execute"] for m in server.received] == ["qmp_capabilities", "query-status"]


def test_qmp_errors_become_provider_errors() -> None:
    server = FakeQmpServer({"error": {"class": "GenericError", "desc": "nope"}})
    with pytest.raises(ProviderError, match="QMP error: nope"):
        QmpClient(server.port).execute("system_powerdown")


@pytest.mark.skipif(
    os.environ.get("VMLM_QEMU_E2E") != "1", reason="set VMLM_QEMU_E2E=1 with QEMU installed"
)
def test_real_qemu_lifecycle(tmp_path: Path) -> None:  # pragma: no cover - manual
    settings = Settings(provider="qemu", qemu_state_dir=str(tmp_path / "state"))
    p = QemuProvider.from_settings(settings)
    p.provision(VM_ID, 1, 512)
    assert p.inspect(VM_ID) is ProviderVMState.RUNNING
    p.stop(VM_ID)
    assert p.inspect(VM_ID) is ProviderVMState.STOPPED
    p.start(VM_ID)
    assert p.inspect(VM_ID) is ProviderVMState.RUNNING
    p.delete(VM_ID)
    assert p.inspect(VM_ID) is None
