from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


class TmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class TmuxSession:
    name: str


def _configured_home_session() -> str:
    """`[tmux].home_session`: when set, local workers live as windows of that
    one session (the user's hand-built shape) instead of a session each.
    The env var override exists so the test suite can pin the legacy shape."""
    env = os.environ.get("BEMINE_TMUX_HOME_SESSION")
    if env is not None:
        return env.strip()
    from ..config import config_string

    return config_string("tmux", "home_session", "").strip()


class TmuxAdapter:
    def __init__(
        self, *, tmux_bin: str = "tmux", home_session: str | None = None
    ) -> None:
        self.tmux_bin = tmux_bin
        if shutil.which(tmux_bin) is None:
            raise TmuxError(f"tmux binary not found: {tmux_bin}")
        self.home_session = (
            _configured_home_session() if home_session is None else home_session.strip()
        )

    def start(self, name: str, command: str = "bash") -> TmuxSession:
        if self.home_session:
            if self._session_exists(self.home_session):
                self._run(
                    ["new-window", "-t", f"{self.home_session}:", "-n", name, command]
                )
            else:
                self._run(
                    ["new-session", "-d", "-s", self.home_session, "-n", name, command]
                )
            return TmuxSession(name=name)
        self._run(["new-session", "-d", "-s", name, command])
        return TmuxSession(name=name)

    def exists(self, name: str) -> bool:
        return self._target(name) is not None

    def send(self, name: str, text: str, *, enter: bool = True) -> None:
        args = ["send-keys", "-t", self._target(name) or name, text]
        if enter:
            args.append("Enter")
        self._run(args)

    def capture(self, name: str) -> str:
        return self._run(
            ["capture-pane", "-p", "-S", "-", "-t", self._target(name) or name]
        ).stdout

    def kill(self, name: str) -> None:
        if self._session_exists(name):
            subprocess.run(
                [self.tmux_bin, "kill-session", "-t", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        window = self._window_id(name)
        if window is not None:
            subprocess.run(
                [self.tmux_bin, "kill-window", "-t", window],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def _target(self, name: str) -> str | None:
        """A worker may live as its own session (legacy) or as a window named
        after it (home-session shape) — wherever the user dragged it."""
        if self._session_exists(name):
            return name
        return self._window_id(name)

    def _session_exists(self, name: str) -> bool:
        proc = subprocess.run(
            [self.tmux_bin, "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0

    def _window_id(self, name: str) -> str | None:
        proc = subprocess.run(
            [self.tmux_bin, "list-windows", "-a", "-F", "#{window_name}\t#{window_id}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            return None
        for line in (proc.stdout or "").splitlines():
            window_name, _, window_id = line.partition("\t")
            if window_name == name and window_id:
                return window_id
        return None

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
