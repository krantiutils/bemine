from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from bemine.mail_bridge import check_proton_bridge, render_proton_bridge_status


def test_proton_bridge_status_reports_ready_when_process_and_ports_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "protonmail-bridge"
    bridge.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    imap = _listen()
    smtp = _listen()
    try:
        status = check_proton_bridge(
            imap_port=imap.getsockname()[1],
            smtp_port=smtp.getsockname()[1],
            runner=_fake_running_bridge,
        )
    finally:
        imap.close()
        smtp.close()

    assert status.ready is True
    assert status.installed is True
    assert status.running is True
    assert status.imap_open is True
    assert status.smtp_open is True
    assert "ready: yes" in render_proton_bridge_status(status)


def test_proton_bridge_status_reports_not_ready_when_closed(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    status = check_proton_bridge(
        imap_port=_free_port(),
        smtp_port=_free_port(),
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""),
    )

    assert status.ready is False
    assert status.installed is False
    assert status.running is False
    assert status.imap_open is False
    assert status.smtp_open is False


def _listen() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def _free_port() -> int:
    sock = _listen()
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _fake_running_bridge(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args,
        0,
        "/usr/lib/protonmail/bridge/bridge --grpc --launcher proton-bridge",
        "",
    )
