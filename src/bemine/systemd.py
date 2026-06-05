from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UnitFile:
    name: str
    text: str


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
