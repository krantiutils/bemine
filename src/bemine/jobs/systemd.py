from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class UnitFile:
    name: str
    text: str


@dataclass(frozen=True)
class UnitStatus:
    name: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    main_pid: int
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "load_state": self.load_state,
            "active_state": self.active_state,
            "sub_state": self.sub_state,
            "unit_file_state": self.unit_file_state,
            "main_pid": self.main_pid,
            "error": self.error,
        }


SYSTEMD_UNITS = ("bemine-daemon.service", "mayor-discord-gateway.service")


def render_user_units(
    *, repo_dir: Path, state_dir: Path, uv_path: str | None = None
) -> list[UnitFile]:
    repo_dir = repo_dir.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    uv_path = (
        uv_path or shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    )
    env_lines = [
        "Environment=PYTHONUNBUFFERED=1",
        f"Environment=BEMINE_HOME={state_dir}",
        f"Environment=BEMINE_CONFIG={repo_dir / 'bemine.toml'}",
    ]
    env_block = "\n".join(env_lines)

    return [
        UnitFile(
            name="bemine-daemon.service",
            text=f"""[Unit]
Description=Bemine background daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo_dir}
{env_block}
ExecStart={uv_path} run bemine-daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
""",
        ),
        UnitFile(
            name="mayor-discord-gateway.service",
            text=f"""[Unit]
Description=Mayor Discord Gateway
After=network-online.target bemine-daemon.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo_dir}
{env_block}
ExecStart={uv_path} run mayor-discord-gateway
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
""",
        ),
    ]


def install_user_units(
    *,
    repo_dir: Path,
    state_dir: Path,
    now: bool,
    enable_linger: bool,
) -> list[Path]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for unit in render_user_units(repo_dir=repo_dir, state_dir=state_dir):
        path = unit_dir / unit.name
        path.write_text(unit.text, encoding="utf-8")
        written.append(path)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        [
            "systemctl",
            "--user",
            "enable",
            "bemine-daemon.service",
            "mayor-discord-gateway.service",
        ],
        check=True,
    )
    if now:
        subprocess.run(
            [
                "systemctl",
                "--user",
                "restart",
                "bemine-daemon.service",
                "mayor-discord-gateway.service",
            ],
            check=True,
        )
    if enable_linger:
        subprocess.run(
            ["loginctl", "enable-linger", os.environ.get("USER", "")], check=False
        )
    return written


def user_unit_status(
    units: tuple[str, ...] = SYSTEMD_UNITS,
    *,
    runner: Callable = subprocess.run,
) -> list[UnitStatus]:
    statuses: list[UnitStatus] = []
    props = "LoadState,ActiveState,SubState,UnitFileState,MainPID"
    for unit in units:
        proc = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                f"--property={props}",
                "--no-page",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            statuses.append(
                UnitStatus(
                    name=unit,
                    load_state="unknown",
                    active_state="unknown",
                    sub_state="unknown",
                    unit_file_state="unknown",
                    main_pid=0,
                    error=(proc.stderr or proc.stdout or "").strip(),
                )
            )
            continue
        data = _parse_systemctl_show(proc.stdout or "")
        statuses.append(
            UnitStatus(
                name=unit,
                load_state=data.get("LoadState", ""),
                active_state=data.get("ActiveState", ""),
                sub_state=data.get("SubState", ""),
                unit_file_state=data.get("UnitFileState", ""),
                main_pid=_int(data.get("MainPID")),
            )
        )
    return statuses


def _parse_systemctl_show(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def _int(value: str | None) -> int:
    try:
        return int(value or "0")
    except ValueError:
        return 0
