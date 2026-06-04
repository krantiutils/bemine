from __future__ import annotations

import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass


DEFAULT_IMAP_HOST = "127.0.0.1"
DEFAULT_IMAP_PORT = 1143
DEFAULT_SMTP_HOST = "127.0.0.1"
DEFAULT_SMTP_PORT = 1025

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ProtonBridgeStatus:
    binary: str
    installed: bool
    running: bool
    imap_host: str
    imap_port: int
    imap_open: bool
    smtp_host: str
    smtp_port: int
    smtp_open: bool

    @property
    def ready(self) -> bool:
        return self.installed and self.running and self.imap_open and self.smtp_open

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ready"] = self.ready
        return data


def check_proton_bridge(
    *,
    imap_host: str = DEFAULT_IMAP_HOST,
    imap_port: int = DEFAULT_IMAP_PORT,
    smtp_host: str = DEFAULT_SMTP_HOST,
    smtp_port: int = DEFAULT_SMTP_PORT,
    timeout: float = 0.5,
    runner: Runner | None = None,
) -> ProtonBridgeStatus:
    binary = shutil.which("protonmail-bridge") or shutil.which("proton-mail-bridge") or ""
    return ProtonBridgeStatus(
        binary=binary,
        installed=bool(binary),
        running=_bridge_process_running(runner=runner),
        imap_host=imap_host,
        imap_port=imap_port,
        imap_open=_tcp_open(imap_host, imap_port, timeout=timeout),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_open=_tcp_open(smtp_host, smtp_port, timeout=timeout),
    )


def render_proton_bridge_status(status: ProtonBridgeStatus) -> str:
    ready = "yes" if status.ready else "no"
    installed = status.binary if status.installed else "not found"
    lines = [
        "Proton Mail Bridge:",
        f"- ready: {ready}",
        f"- binary: {installed}",
        f"- process running: {'yes' if status.running else 'no'}",
        f"- IMAP: {status.imap_host}:{status.imap_port} {'open' if status.imap_open else 'closed'}",
        f"- SMTP: {status.smtp_host}:{status.smtp_port} {'open' if status.smtp_open else 'closed'}",
    ]
    if not status.ready:
        lines.append("- next: start Proton Mail Bridge and confirm IMAP/SMTP are enabled.")
    return "\n".join(lines) + "\n"


def _bridge_process_running(*, runner: Runner | None = None) -> bool:
    runner = runner or subprocess.run
    proc = runner(
        ["pgrep", "-af", "proton.*bridge|bridge.*grpc"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return False
    output = (proc.stdout or "").lower()
    return "proton" in output and "bridge" in output


def _tcp_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
