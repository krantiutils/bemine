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

    def __init__(
        self,
        *,
        gateway_device: str,
        runner: Runner | None = None,
        timeout: int = 15,
        pause=None,
        remote_shell: str = "zsh -lic",
    ) -> None:
        self.gateway_device = gateway_device
        self._runner = runner or self._default_runner
        self.timeout = timeout
        self._pause = pause or time.sleep
        self.remote_shell = remote_shell

    def _default_runner(self, argv, **kwargs):
        return subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
            **kwargs,
        )

    def _argv(self, device: str, tmux_args: list[str]) -> list[str]:
        if device == self.gateway_device or device == "local":
            return ["tmux", *tmux_args]
        remote_cmd = f"{self.remote_shell} " + shlex.quote(
            shlex.join(["tmux", *tmux_args])
        )
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            device,
            remote_cmd,
        ]

    def _run(self, device: str, tmux_args: list[str]) -> str:
        argv = self._argv(device, tmux_args)
        proc = self._runner(argv)
        if proc.returncode != 0:
            raise RemoteTmuxError(
                (proc.stderr or "").strip() or f"tmux failed: {' '.join(argv)}"
            )
        return proc.stdout or ""

    def list_panes(self, device: str) -> list[dict]:
        out = self._run(
            device,
            [
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{pane_current_path}\t#{pane_current_command}"
                "\t#{window_activity}\t#{session_attached}",
            ],
        )
        panes: list[dict] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue

            def _int(index: int) -> int:
                try:
                    return int(parts[index])
                except (ValueError, TypeError, IndexError):
                    return 0

            panes.append(
                {
                    "pane_id": parts[0],
                    "path": parts[1],
                    "command": parts[2],
                    "activity": _int(3),
                    "attached": _int(4),
                }
            )
        return panes

    def capture(self, device: str, pane_id: str, lines: int = 200) -> str:
        return self._run(
            device, ["capture-pane", "-p", "-S", f"-{lines}", "-t", pane_id]
        )

    def send_keys(self, device: str, pane_id: str, keys: list[str]) -> None:
        self._run(device, ["send-keys", "-t", pane_id, *keys])
        if keys and keys[-1] == "Enter":
            # TUIs eat an Enter mid-redraw (an overnight auto-answer sat
            # unsubmitted in the input box); a duplicate Enter on an empty
            # input is a no-op, so always chase with one.
            self._pause(1.5)
            self._run(device, ["send-keys", "-t", pane_id, "Enter"])

    def send_text(self, device: str, pane_id: str, text: str) -> None:
        # -l sends text literally; pause so the TUI absorbs it before Enter submits.
        self._run(device, ["send-keys", "-t", pane_id, "-l", text])
        self._pause(1.0)
        self._run(device, ["send-keys", "-t", pane_id, "Enter"])
        self._pause(2.0)
        self._run(device, ["send-keys", "-t", pane_id, "Enter"])
