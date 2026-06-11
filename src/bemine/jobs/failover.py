"""Cross-device failover: revive a dead device's managed workers locally.

A power cut on a remote device kills its tmux server; continuity only covers
the daemon's own device, so those workers stayed dead. This pass probes each
device that hosts running managed workers; after N consecutive unreachable
passes it re-creates each worker on THIS device — matching local checkout,
fresh pinned CLI conversation, and a revival brief carrying the last-known
project state. The original conversation transcript lives on the dead disk and
cannot be resumed by id; the brief is explicit about that.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from bemine.config import BemineSettings, config_bool, config_int
from bemine.db import BemineDB
from bemine.workers.workers import WorkerController, _validate_name

DEFAULT_UNREACHABLE_PASSES = 3


def failover_state_path(settings: BemineSettings) -> Path:
    return settings.state_dir / "failover_state.json"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _default_probe(device: str) -> bool:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", device, "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def _local_cwd_for(
    db: BemineDB, settings: BemineSettings, row: dict[str, Any]
) -> Path | None:
    mapped = db.device_cwd_for_project(
        str(row["project_id"]), device_id=settings.device_id
    )
    if mapped:
        path = Path(mapped).expanduser()
        if path.is_dir():
            return path
    same = Path(str(row["cwd"])).expanduser()
    if same.is_dir():
        return same
    return None


def _revival_brief(settings: BemineSettings, row: dict[str, Any]) -> str:
    from bemine.context.project_state import project_state_context

    try:
        state = project_state_context(
            settings,
            project_id=str(row["project_id"]),
            cwd=Path(str(row["cwd"])),
        )
    except Exception:
        state = "(project state unavailable)"
    return "\n".join(
        [
            "# Bemine Failover Revival",
            f"The device `{row['device_id']}` lost power/network; its worker "
            f"`{row['name']}` ({row['runtime']}) died with it. You are its "
            f"replacement on `{settings.device_id}`.",
            "The original conversation transcript is on the dead device and is "
            "NOT available — rebuild your bearings from the project state below, "
            "then continue the work. Check git status/log first; the dead worker "
            "may have uncommitted progress that only exists on the dead device.",
            "",
            "Last-known project state:",
            _revival_state_or_placeholder(state),
        ]
    )


def _revival_state_or_placeholder(state: str) -> str:
    return state.strip() or "(no recorded project state)"


def tick_device_failover(
    settings: BemineSettings,
    *,
    db: BemineDB | None = None,
    controller: WorkerController | None = None,
    probe: Callable[[str], bool] = _default_probe,
    send_sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    if not config_bool("failover", "enabled", True):
        return []
    db = db or BemineDB(settings.db_path)
    controller = controller or WorkerController(settings, db=db)
    threshold = max(
        1, config_int("failover", "unreachable_passes", DEFAULT_UNREACHABLE_PASSES)
    )
    path = failover_state_path(settings)
    state = _load_state(path)
    revived_map = state.setdefault("revived", {})
    failures = state.setdefault("failures", {})

    remote_workers: dict[str, list[dict[str, Any]]] = {}
    for row in db.list_managed_sessions():
        if str(row["transport"]) != "ssh-tmux":
            continue
        if str(row["status"]) not in {"running", "adopted"}:
            continue
        device = str(row["device_id"])
        if device in {settings.device_id, "local"}:
            continue
        remote_workers.setdefault(device, []).append(dict(row))

    revived: list[dict[str, Any]] = []
    for device, rows in sorted(remote_workers.items()):
        try:
            reachable = bool(probe(device))
        except Exception:
            reachable = False
        if reachable:
            if int(failures.get(device, 0)) >= threshold:
                db.record_audit_event(
                    category="failover",
                    event_type="device_back",
                    actor="failover",
                    device_id=settings.device_id,
                    payload_json=json.dumps(
                        {"device": device, "note": "check for duplicate heads"},
                        sort_keys=True,
                    ),
                )
            failures[device] = 0
            continue
        failures[device] = int(failures.get(device, 0)) + 1
        if failures[device] < threshold:
            continue
        for row in rows:
            original = str(row["name"])
            if revived_map.get(original):
                continue
            command = str(row["command"] or "").strip()
            if not command or command == "adopted-existing-tmux-session":
                revived_map[original] = "skipped:no-restartable-command"
                continue
            local_cwd = _local_cwd_for(db, settings, row)
            if local_cwd is None:
                revived_map[original] = "skipped:no-local-checkout"
                db.record_audit_event(
                    category="failover",
                    event_type="worker_failover_skipped",
                    actor="failover",
                    device_id=settings.device_id,
                    project_id=str(row["project_id"]),
                    payload_json=json.dumps(
                        {"name": original, "device": device, "cwd": str(row["cwd"])},
                        sort_keys=True,
                    ),
                )
                continue
            replacement = _validate_name(f"{original}-fo")
            try:
                record = controller.start(
                    name=replacement,
                    runtime=str(row["runtime"]),
                    cwd=local_cwd,
                    command=command,
                )
                controller.send_when_ready(
                    replacement, _revival_brief(settings, row), sleep=send_sleep
                )
            except Exception as exc:
                revived_map[original] = f"failed:{exc}"
                continue
            db.set_managed_session_status(original, status="failed-over")
            revived_map[original] = replacement
            db.record_audit_event(
                category="failover",
                event_type="worker_failover_revived",
                actor="failover",
                device_id=settings.device_id,
                project_id=str(row["project_id"]),
                payload_json=json.dumps(
                    {
                        "original": original,
                        "replacement": replacement,
                        "dead_device": device,
                        "cwd": str(local_cwd),
                        "runtime_session_id": record.runtime_session_id,
                    },
                    sort_keys=True,
                ),
            )
            revived.append({"original": original, "replacement": replacement})

    _save_state(path, state)
    return revived
