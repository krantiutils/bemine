from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class TmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class TmuxSession:
    name: str


class TmuxAdapter:
    def __init__(self, *, tmux_bin: str = "tmux") -> None:
        self.tmux_bin = tmux_bin
        if shutil.which(tmux_bin) is None:
            raise TmuxError(f"tmux binary not found: {tmux_bin}")

    def start(self, name: str, command: str = "bash") -> TmuxSession:
        self._run(["new-session", "-d", "-s", name, command])
        return TmuxSession(name=name)

    def exists(self, name: str) -> bool:
        proc = subprocess.run(
            [self.tmux_bin, "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0

    def send(self, name: str, text: str, *, enter: bool = True) -> None:
        args = ["send-keys", "-t", name, text]
        if enter:
            args.append("Enter")
        self._run(args)

    def capture(self, name: str) -> str:
        return self._run(["capture-pane", "-p", "-S", "-", "-t", name]).stdout

    def kill(self, name: str) -> None:
        subprocess.run(
            [self.tmux_bin, "kill-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [self.tmux_bin, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            raise TmuxError(proc.stderr.strip() or f"tmux failed: {' '.join(args)}")
        return proc
