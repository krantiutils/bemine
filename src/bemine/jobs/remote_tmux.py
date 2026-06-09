from __future__ import annotations

import shlex
import subprocess
import time
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class RemoteTmuxError(RuntimeError):
    pass


class RemoteTmux:
    """Run tmux commands locally (gateway device) or over SSH (any other device).

    The subprocess runner is injectable so callers and tests can substitute it.
    """

    def __init__(self, *, gateway_device: str, runner: Runner | None = None,
                 timeout: int = 15, pause=None, remote_shell: str = "zsh -lic") -> None:
        self.gateway_device = gateway_device
        self._runner = runner or self._default_runner
        self.timeout = timeout
        self._pause = pause or time.sleep
        self.remote_shell = remote_shell

    def _default_runner(self, argv, **kwargs):
        return subprocess.run(
            argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=self.timeout, check=False, **kwargs
        )

    def _argv(self, device: str, tmux_args: list[str]) -> list[str]:
        if device == self.gateway_device or device == "local":
            return ["tmux", *tmux_args]
        remote_cmd = f"{self.remote_shell} " + shlex.quote(shlex.join(["tmux", *tmux_args]))
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", device, remote_cmd]

    def _run(self, device: str, tmux_args: list[str]) -> str:
        argv = self._argv(device, tmux_args)
        proc = self._runner(argv)
        if proc.returncode != 0:
            raise RemoteTmuxError(
                (proc.stderr or "").strip() or f"tmux failed: {' '.join(argv)}"
            )
        return proc.stdout or ""

    def list_panes(self, device: str) -> list[dict]:
        out = self._run(device, [
            "list-panes", "-a", "-F",
            "#{pane_id}\t#{pane_current_path}\t#{pane_current_command}",
        ])
        panes: list[dict] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            panes.append({"pane_id": parts[0], "path": parts[1], "command": parts[2]})
        return panes

    def capture(self, device: str, pane_id: str) -> str:
        return self._run(device, ["capture-pane", "-p", "-t", pane_id])

    def send_keys(self, device: str, pane_id: str, keys: list[str]) -> None:
        self._run(device, ["send-keys", "-t", pane_id, *keys])

    def send_text(self, device: str, pane_id: str, text: str) -> None:
        # -l sends text literally; pause so the TUI absorbs it before Enter submits.
        self._run(device, ["send-keys", "-t", pane_id, "-l", text])
        self._pause(0.2)
        self._run(device, ["send-keys", "-t", pane_id, "Enter"])
