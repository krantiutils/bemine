"""Snapshot/restore of hand-built tmux layouts across reboots.

The user's daily driver is ONE tmux session with several windows, each window
an agent CLI sitting in its project cwd. Managed workers already come back via
resume_missing_local_workers; these tests cover everything Bemine did NOT
start itself."""

from __future__ import annotations

import json
import subprocess

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.workers import layout as layout_mod
from bemine.workers.layout import (
    layout_state_path,
    restore_tmux_layout,
    snapshot_tmux_layout,
)

PANE_LINES = "\n".join(
    [
        "hub\t0\tbm\t/home/cdjk/bm\tclaude",
        "hub\t1\ttts\t/home/cdjk/github/streaming-tts\tclaude",
        "hub\t1\ttts\t/home/cdjk/github/streaming-tts\tbash",  # split pane: keep first
        "hub\t2\tlogs\t/home/cdjk\tbash",
        "hub\t3\tbm-blog\t/home/cdjk/bm\tclaude",  # managed worker AS A WINDOW
        "bm-blog\t0\tbm-blog\t/home/cdjk/bm\tclaude",  # managed worker session
    ]
)


def _settings(tmp_path) -> BemineSettings:
    return BemineSettings.load(
        state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "vault", device_id="cdjk"
    )


def _db_with_managed_worker(settings) -> BemineDB:
    db = BemineDB(settings.db_path)
    db.upsert_managed_session(
        name="bm-blog",
        runtime="claude",
        transport="tmux",
        device_id="cdjk",
        project_id="proj_x",
        cwd="/home/cdjk/bm",
        command="claude",
        status="running",
    )
    return db


def _ok_runner(stdout: str = ""):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def test_snapshot_groups_windows_and_skips_managed(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = _db_with_managed_worker(settings)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 1000.0)

    snap = snapshot_tmux_layout(settings, db=db, runner=_ok_runner(PANE_LINES))

    assert snap is not None
    saved = json.loads(layout_state_path(settings).read_text(encoding="utf-8"))
    assert set(saved["sessions"]) == {"hub"}  # managed bm-blog excluded
    windows = saved["sessions"]["hub"]["windows"]
    # worker continuity owns bm-blog whether it is a session OR a hub window
    assert [w["index"] for w in windows] == [0, 1, 2]
    assert windows[0] == {
        "index": 0,
        "name": "bm",
        "cwd": "/home/cdjk/bm",
        "command": "claude",
    }
    assert windows[1]["command"] == "claude"  # first pane of the split window wins
    assert saved["ts"] > 0


def test_snapshot_no_server_keeps_existing_file(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = _db_with_managed_worker(settings)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 1000.0)
    path = layout_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": 5.0, "sessions": {"hub": {"windows": []}}}))

    def no_server(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="no server running"
        )

    snap = snapshot_tmux_layout(settings, db=db, runner=no_server)

    assert snap is None
    assert json.loads(path.read_text())["sessions"] == {"hub": {"windows": []}}


def test_snapshot_refuses_to_clobber_pending_restore(tmp_path, monkeypatch):
    """After a reboot the daemon resumes managed workers FIRST, which starts the
    tmux server — a snapshot then would record only those and lose the layout."""
    settings = _settings(tmp_path)
    db = _db_with_managed_worker(settings)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 2000.0)
    path = layout_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    pre_boot = {"ts": 1500.0, "sessions": {"hub": {"windows": []}}}
    path.write_text(json.dumps(pre_boot))

    snap = snapshot_tmux_layout(settings, db=db, runner=_ok_runner(PANE_LINES))

    assert snap is None
    assert json.loads(path.read_text()) == pre_boot


def test_restore_recreates_windows_with_resume_and_shell(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = _db_with_managed_worker(settings)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 2000.0)
    monkeypatch.setattr(
        layout_mod,
        "_resume_launch_command",
        lambda db, settings, *, runtime, cwd, command: (
            (f"{command} --resume abc123", "abc123") if cwd == "/home/cdjk/bm" else None
        ),
    )
    monkeypatch.setattr(
        layout_mod, "_launchable_command", lambda runtime, command: command
    )
    nudged: list[str] = []
    monkeypatch.setattr(
        layout_mod, "_nudge_window", lambda target, sleep: nudged.append(target)
    )
    path = layout_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": 1500.0,
                "sessions": {
                    "hub": {
                        "windows": [
                            {
                                "index": 0,
                                "name": "bm",
                                "cwd": "/home/cdjk/bm",
                                "command": "claude",
                            },
                            {
                                "index": 2,
                                "name": "logs",
                                "cwd": "/home/cdjk",
                                "command": "bash",
                            },
                        ]
                    },
                    "bm-blog": {"windows": []},  # managed: never layout-restored
                },
            }
        )
    )
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    restored = restore_tmux_layout(settings, db=db, runner=runner, nudge_sleep=None)

    assert [r["session"] for r in restored] == ["hub"]
    new_session = next(c for c in calls if "new-session" in c)
    assert new_session[new_session.index("-s") + 1] == "hub"
    assert new_session[new_session.index("-c") + 1] == "/home/cdjk/bm"
    assert new_session[-1] == "claude --resume abc123"
    new_window = next(c for c in calls if "new-window" in c)
    assert new_window[new_window.index("-c") + 1] == "/home/cdjk"
    assert new_window[-1] == "/home/cdjk"  # no command arg: default shell
    assert nudged == ["hub:0"]
    # marker prevents a second restore this boot
    assert json.loads(path.read_text())["restored_boot"] == 2000.0
    again = restore_tmux_layout(settings, db=db, runner=runner, nudge_sleep=None)
    assert again == []


def test_restore_skips_fresh_snapshot_and_live_sessions(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 2000.0)
    path = layout_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    # snapshot taken AFTER boot: the server died some other way (user quit tmux)
    path.write_text(json.dumps({"ts": 2500.0, "sessions": {"hub": {"windows": []}}}))
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    assert restore_tmux_layout(settings, db=db, runner=runner, nudge_sleep=None) == []
    assert calls == []

    # pre-boot snapshot, session alive, every window still there -> untouched
    path.write_text(
        json.dumps(
            {
                "ts": 1500.0,
                "sessions": {
                    "hub": {
                        "windows": [
                            {"index": 0, "name": "bm", "cwd": "/x", "command": "bash"}
                        ]
                    }
                },
            }
        )
    )
    calls.clear()

    def live_runner(args, **kwargs):
        calls.append(list(args))
        stdout = "bm\n" if "list-windows" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    assert (
        restore_tmux_layout(settings, db=db, runner=live_runner, nudge_sleep=None) == []
    )
    assert all("new-session" not in c and "new-window" not in c for c in calls)
    assert json.loads(path.read_text())["restored_boot"] == 2000.0


def test_restore_merges_missing_windows_into_live_session(tmp_path, monkeypatch):
    """A surviving session is no longer skipped wholesale: windows that died
    (e.g. only part of the layout came back) are recreated inside it."""
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 2000.0)
    path = layout_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": 1500.0,
                "sessions": {
                    "hub": {
                        "windows": [
                            {"index": 0, "name": "bm", "cwd": "/a", "command": "bash"},
                            {"index": 1, "name": "logs", "cwd": "/b", "command": "bash"},
                        ]
                    }
                },
            }
        )
    )
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        stdout = "bm\n" if "list-windows" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    restored = restore_tmux_layout(settings, db=db, runner=runner, nudge_sleep=None)

    assert restored == [{"session": "hub", "windows": 1, "resumed": []}]
    assert all("new-session" not in c for c in calls)  # session already lives
    new_window = next(c for c in calls if "new-window" in c)
    assert new_window[new_window.index("-t") + 1] == "hub:"  # append, index taken
    assert new_window[new_window.index("-n") + 1] == "logs"
    assert new_window[new_window.index("-c") + 1] == "/b"


def test_restore_never_reruns_arbitrary_commands(tmp_path, monkeypatch):
    """Only known agent CLIs are relaunched; anything else gets a plain shell
    in the right cwd — rerunning e.g. an old rm/rsync would be destructive."""
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    monkeypatch.setattr(layout_mod, "_boot_epoch", lambda: 2000.0)
    path = layout_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": 1500.0,
                "sessions": {
                    "hub": {
                        "windows": [
                            {
                                "index": 0,
                                "name": "danger",
                                "cwd": "/home/cdjk",
                                "command": "rsync",
                            }
                        ]
                    }
                },
            }
        )
    )
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        rc = 1 if "has-session" in args else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

    restore_tmux_layout(settings, db=db, runner=runner, nudge_sleep=None)

    new_session = next(c for c in calls if "new-session" in c)
    assert "rsync" not in new_session  # cwd-only shell window
    assert new_session[-1] == "/home/cdjk"
