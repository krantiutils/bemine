"""Workers live as windows of ONE home session.

The user's daily shape is a single tmux session (`0`) with one window per
agent. With `[tmux].home_session` set, the adapter starts workers as windows
of that session and resolves worker names to either a session (legacy) or a
window, so send/capture/kill/exists keep working after the shape change.
"""

from __future__ import annotations

import subprocess
import time
import uuid

from bemine.workers.tmux_adapter import TmuxAdapter


def _names() -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"bemine-home-{suffix}", f"bemine-w1-{suffix}", f"bemine-w2-{suffix}"


def _wait_for(tmux: TmuxAdapter, name: str, needle: str, timeout: float = 30) -> str:
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        text = tmux.capture(name)
        if needle in text:
            return text
        time.sleep(0.1)
    return text


def _session_names() -> set[str]:
    proc = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return set((proc.stdout or "").split())


def _kill_session(name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def test_home_session_workers_are_windows_not_sessions():
    home, w1, w2 = _names()
    tmux = TmuxAdapter(home_session=home)
    try:
        tmux.start(w1, "bash")
        tmux.start(w2, "bash")

        sessions = _session_names()
        assert home in sessions
        assert w1 not in sessions and w2 not in sessions  # windows, not sessions
        windows = tmux._run(
            ["list-windows", "-t", home, "-F", "#{window_name}"]
        ).stdout.split()
        assert set(windows) == {w1, w2}

        # name resolution: exists/send/capture address the right window
        assert tmux.exists(w1) and tmux.exists(w2)
        tmux.send(w1, "printf 'MARKER_ONE\\n'")
        tmux.send(w2, "printf 'MARKER_TWO\\n'")
        assert "MARKER_ONE" in _wait_for(tmux, w1, "MARKER_ONE")
        assert "MARKER_TWO" in _wait_for(tmux, w2, "MARKER_TWO")
        assert "MARKER_TWO" not in tmux.capture(w1)

        # kill removes only that worker's window; the home session survives
        tmux.kill(w1)
        assert not tmux.exists(w1)
        assert tmux.exists(w2)
        assert home in _session_names()
    finally:
        _kill_session(home)


def test_home_session_mode_still_finds_legacy_session_workers():
    """A worker that predates the shape change lives as its own session; the
    adapter must keep addressing it by session name."""
    home, legacy, _ = _names()
    tmux = TmuxAdapter(home_session=home)
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", legacy, "bash"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert tmux.exists(legacy)
        tmux.send(legacy, "printf 'LEGACY_OK\\n'")
        assert "LEGACY_OK" in _wait_for(tmux, legacy, "LEGACY_OK")
        tmux.kill(legacy)
        assert not tmux.exists(legacy)
    finally:
        _kill_session(legacy)
        _kill_session(home)


def test_empty_home_session_keeps_one_session_per_worker():
    _, w1, _ = _names()
    tmux = TmuxAdapter(home_session="")
    try:
        tmux.start(w1, "bash")
        assert w1 in _session_names()
    finally:
        _kill_session(w1)
