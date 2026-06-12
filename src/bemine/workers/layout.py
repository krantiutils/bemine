"""Snapshot and restore hand-built tmux layouts across reboots.

`resume_missing_local_workers` only revives sessions Bemine started itself —
it knows their launch commands. The layout the user builds by hand (one
session, several windows, each window an agent CLI in its project cwd) used
to die with the machine. This module closes that gap:

- every continuity tick snapshots the live tmux tree to
  `state_dir/continuity/tmux_layout.json`;
- after a reboot (snapshot older than the kernel boot time, once per boot)
  each missing session is recreated window by window: known agent CLIs are
  relaunched with their runtime-native resume when the sessions index knows
  the conversation for that cwd, everything else gets a plain shell in the
  recorded cwd. Arbitrary commands are NEVER re-run — replaying an old
  rsync/rm would be destructive.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.workers.workers import (
    RESUME_NUDGE,
    _launchable_command,
    _resume_launch_command,
    send_to_cli_when_ready,
)

# Only these are ever relaunched from a snapshot; each resumes its own
# conversation, so a relaunch is safe and idempotent.
LAYOUT_RUNTIMES = ("claude", "codex", "kimi")

PANE_FORMAT = (
    "#{session_name}\t#{window_index}\t#{window_name}"
    "\t#{pane_current_path}\t#{pane_current_command}"
)


def layout_state_path(settings: BemineSettings) -> Path:
    return settings.state_dir / "continuity" / "tmux_layout.json"


def _boot_epoch() -> float:
    """Kernel boot time; a snapshot older than this died with the machine."""
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        return time.time() - uptime
    except (OSError, ValueError, IndexError):
        return 0.0


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _restore_pending(state: dict[str, Any], boot: float) -> bool:
    ts = float(state.get("ts") or 0.0)
    return (
        bool(state.get("sessions")) and ts < boot and state.get("restored_boot") != boot
    )


def _managed_names(db: BemineDB) -> set[str]:
    return {str(row["name"]) for row in db.list_managed_sessions()}


def snapshot_tmux_layout(
    settings: BemineSettings,
    *,
    db: BemineDB | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any] | None:
    """Record the live tmux tree; managed workers are excluded (they have
    their own continuity). Returns None when there is nothing safe to write:
    no tmux server, or a pre-boot snapshot is still waiting to be restored —
    overwriting that would lose the layout to a post-boot, workers-only view.
    """
    db = db or BemineDB(settings.db_path)
    path = layout_state_path(settings)
    existing = _load_state(path)
    if _restore_pending(existing, _boot_epoch()):
        return None
    proc = runner(
        ["tmux", "list-panes", "-a", "-F", PANE_FORMAT],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return None
    managed = _managed_names(db)
    sessions: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, int]] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, raw_index, window_name, cwd, command = parts[:5]
        if name in managed or window_name in managed:
            # worker continuity owns managed workers, whether each is its own
            # session (legacy) or a window of the home session
            continue
        try:
            index = int(raw_index)
        except ValueError:
            continue
        if (name, index) in seen:
            continue  # split panes: the first (top-left) pane describes the window
        seen.add((name, index))
        sessions.setdefault(name, {"windows": []})["windows"].append(
            {"index": index, "name": window_name, "cwd": cwd, "command": command}
        )
    for info in sessions.values():
        info["windows"].sort(key=lambda w: w["index"])
    state = {
        "ts": time.time(),
        "restored_boot": existing.get("restored_boot"),
        "sessions": sessions,
    }
    _save_state(path, state)
    return state


def _nudge_window(target: str, sleep: Callable[[float], None]) -> None:
    from bemine.workers.tmux_adapter import TmuxAdapter

    send_to_cli_when_ready(TmuxAdapter(), target, RESUME_NUDGE, sleep=sleep)


def _window_launch(
    db: BemineDB, settings: BemineSettings, window: dict[str, Any]
) -> tuple[str | None, str]:
    """(command, resumed_session_id) for a recreated window; command None
    means the user's default shell."""
    runtime = str(window.get("command") or "").strip().lower()
    if runtime not in LAYOUT_RUNTIMES:
        return None, ""
    resume = _resume_launch_command(
        db, settings, runtime=runtime, cwd=str(window.get("cwd") or ""), command=runtime
    )
    if resume is not None:
        command, session_id = resume
        return _launchable_command(runtime, command), session_id
    return _launchable_command(runtime, runtime), ""


def restore_tmux_layout(
    settings: BemineSettings,
    *,
    db: BemineDB | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    nudge_sleep: Callable[[float], None] = time.sleep,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Recreate snapshotted sessions that died with the machine, once per boot.

    Sessions that still exist are left alone; managed workers are skipped
    (worker continuity owns them). `force=True` drops the boot-time guards for
    a manual `bemine tmux layout --restore`."""
    db = db or BemineDB(settings.db_path)
    path = layout_state_path(settings)
    state = _load_state(path)
    sessions = state.get("sessions") or {}
    boot = _boot_epoch()
    if not sessions:
        return []
    if not force:
        if float(state.get("ts") or 0.0) >= boot:
            return []  # snapshot postdates the boot: nothing died with the machine
        if state.get("restored_boot") == boot:
            return []
    managed = _managed_names(db)
    restored: list[dict[str, Any]] = []
    for name, info in sessions.items():
        if name in managed:
            continue
        windows = sorted(info.get("windows") or [], key=lambda w: int(w["index"]))
        if not windows:
            continue
        exists = runner(
            ["tmux", "has-session", "-t", name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        session_alive = exists.returncode == 0
        live_windows: set[str] = set()
        if session_alive:
            # the session survived (or another restorer built it first): merge
            # only the windows that are missing, matched by window name
            listed = runner(
                ["tmux", "list-windows", "-t", name, "-F", "#{window_name}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if listed.returncode != 0:
                continue
            live_windows = set((listed.stdout or "").splitlines())
        resumed: list[str] = []
        created = 0
        session_created = session_alive
        for window in windows:
            window_name = str(window.get("name") or "")
            if session_alive and window_name in live_windows:
                continue
            command, session_id = _window_launch(db, settings, window)
            cwd = str(window.get("cwd") or "")
            if not session_created:
                # the recorded first index IS this config's base-index, so
                # new-session lands the window exactly where it was
                argv = ["tmux", "new-session", "-d", "-s", name]
                target = f"{name}:{window['index']}"
            elif session_alive:
                # merging into a live session: recorded indexes may be taken
                argv = ["tmux", "new-window", "-t", f"{name}:"]
                target = f"{name}:{window_name}" if window_name else f"{name}:"
            else:
                argv = ["tmux", "new-window", "-t", f"{name}:{window['index']}"]
                target = f"{name}:{window['index']}"
            if window_name:
                argv += ["-n", window_name]
            argv += ["-c", cwd]
            if command:
                argv.append(command)
            proc = runner(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if proc.returncode != 0:
                if not session_created:
                    break  # session itself failed; leave the rest for next boot
                continue
            session_created = True
            created += 1
            if session_id:
                resumed.append(target)
                try:
                    _nudge_window(target, nudge_sleep)
                except Exception:
                    pass
        if created:
            restored.append({"session": name, "windows": created, "resumed": resumed})
    state["restored_boot"] = boot
    _save_state(path, state)
    if restored:
        db.record_audit_event(
            category="continuity",
            event_type="continuity_layout_restored",
            actor="bemine-daemon",
            device_id=settings.device_id,
            payload_json=json.dumps({"sessions": restored}, sort_keys=True),
        )
    return restored
