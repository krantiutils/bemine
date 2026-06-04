from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bemine.tmux_adapter import TmuxAdapter


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--device",
            "testbox",
            *args,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_workers_start_send_capture_list_and_stop(tmp_path: Path) -> None:
    name = f"bemine-worker-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "repo"
    project.mkdir()

    try:
        started = json.loads(
            _run(
                tmp_path,
                "workers",
                "start",
                name,
                "--runtime",
                "shell",
                "--cwd",
                str(project),
                "--command",
                "bash",
                "--json",
            ).stdout
        )
        assert started["name"] == name
        assert started["runtime"] == "shell"
        assert started["status"] == "running"
        assert started["exists"] is True

        _run(tmp_path, "workers", "send", name, "printf 'WORKER_READY\\n'")
        captured = _capture_until(tmp_path, name, "WORKER_READY")
        assert "WORKER_READY" in captured

        listed = json.loads(_run(tmp_path, "workers", "list", "--json").stdout)
        assert listed[0]["name"] == name
        assert listed[0]["exists"] is True

        stopped = json.loads(_run(tmp_path, "workers", "stop", name, "--json").stdout)
        assert stopped["status"] == "stopped"
        assert stopped["exists"] is False

        forgotten = json.loads(_run(tmp_path, "workers", "forget", name, "--json").stdout)
        assert forgotten == {"forgotten": True, "name": name}
        assert json.loads(_run(tmp_path, "workers", "list", "--json").stdout) == []
    finally:
        TmuxAdapter().kill(name)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_workers_can_adopt_existing_tmux_session(tmp_path: Path) -> None:
    name = f"bemine-adopt-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "repo"
    project.mkdir()
    tmux = TmuxAdapter()
    try:
        tmux.start(name, "bash")
        adopted = json.loads(
            _run(
                tmp_path,
                "workers",
                "adopt",
                name,
                "--runtime",
                "claude",
                "--cwd",
                str(project),
                "--json",
            ).stdout
        )
        assert adopted["name"] == name
        assert adopted["runtime"] == "claude"
        assert adopted["status"] == "adopted"

        _run(tmp_path, "workers", "send", name, "printf 'ADOPTED_READY\\n'")
        captured = _capture_until(tmp_path, name, "ADOPTED_READY")
        assert "ADOPTED_READY" in captured
    finally:
        tmux.kill(name)


def _capture_until(tmp_path: Path, name: str, needle: str) -> str:
    deadline = time.time() + 5
    captured = ""
    while time.time() < deadline:
        captured = _run(tmp_path, "workers", "capture", name).stdout
        if needle in captured:
            return captured
        time.sleep(0.1)
    return captured
