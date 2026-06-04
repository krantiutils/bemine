from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .config import BemineSettings
from .db import BemineDB
from .tmux_adapter import TmuxAdapter
from .util import project_id_from_cwd


WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
DEFAULT_RUNTIME_COMMANDS = {
    "shell": "bash",
    "codex": "codex --yolo",
    "claude": "claude --dangerously-skip-permissions",
    "kimi": "kimi",
}


@dataclass(frozen=True)
class WorkerRecord:
    name: str
    runtime: str
    transport: str
    device_id: str
    project_id: str
    cwd: str
    command: str
    status: str
    exists: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "runtime": self.runtime,
            "transport": self.transport,
            "device_id": self.device_id,
            "project_id": self.project_id,
            "cwd": self.cwd,
            "command": self.command,
            "status": self.status,
            "exists": self.exists,
        }


class WorkerError(RuntimeError):
    pass


class WorkerController:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        db: BemineDB | None = None,
        tmux: TmuxAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.db = db or BemineDB(settings.db_path)
        self.tmux = tmux or TmuxAdapter()

    def start(
        self,
        *,
        name: str,
        runtime: str,
        cwd: Path,
        command: str = "",
    ) -> WorkerRecord:
        name = _validate_name(name)
        cwd = cwd.expanduser().resolve()
        runtime = _normalize_runtime(runtime)
        command = command.strip() or DEFAULT_RUNTIME_COMMANDS.get(runtime, runtime)
        if self.tmux.exists(name):
            raise WorkerError(f"tmux session already exists: {name}")
        tmux_command = _command_in_cwd(cwd, command)
        self.tmux.start(name, tmux_command)
        return self._record(
            name=name,
            runtime=runtime,
            cwd=cwd,
            command=command,
            status="running",
        )

    def adopt(
        self,
        *,
        name: str,
        runtime: str,
        cwd: Path,
        command: str = "adopted-existing-tmux-session",
    ) -> WorkerRecord:
        name = _validate_name(name)
        cwd = cwd.expanduser().resolve()
        runtime = _normalize_runtime(runtime)
        if not self.tmux.exists(name):
            raise WorkerError(f"tmux session does not exist: {name}")
        return self._record(
            name=name,
            runtime=runtime,
            cwd=cwd,
            command=command,
            status="adopted",
        )

    def list(self) -> list[WorkerRecord]:
        records = []
        for row in self.db.list_managed_sessions():
            exists = self.tmux.exists(str(row["name"])) if row["transport"] == "tmux" else False
            status = "running" if exists and row["status"] in {"running", "adopted"} else row["status"]
            if not exists and row["status"] in {"running", "adopted"}:
                status = "missing"
                self.db.set_managed_session_status(str(row["name"]), status=status)
            records.append(
                WorkerRecord(
                    name=str(row["name"]),
                    runtime=str(row["runtime"]),
                    transport=str(row["transport"]),
                    device_id=str(row["device_id"]),
                    project_id=str(row["project_id"]),
                    cwd=str(row["cwd"]),
                    command=str(row["command"]),
                    status=status,
                    exists=exists,
                )
            )
        return records

    def send(self, name: str, text: str, *, enter: bool = True) -> WorkerRecord:
        record = self._require_record(name)
        self.tmux.send(record.name, text, enter=enter)
        self.db.set_managed_session_status(record.name, status="running")
        self._audit("worker_sent", record, {"enter": enter, "text_len": len(text)})
        return record

    def capture(self, name: str) -> str:
        record = self._require_record(name)
        captured = self.tmux.capture(record.name)
        self.db.set_managed_session_status(record.name, status="running")
        self._audit("worker_captured", record, {"chars": len(captured)})
        return captured

    def stop(self, name: str) -> WorkerRecord:
        record = self._require_record(name)
        self.tmux.kill(record.name)
        self.db.set_managed_session_status(record.name, status="stopped")
        stopped = WorkerRecord(**{**record.to_dict(), "status": "stopped", "exists": False})
        self._audit("worker_stopped", stopped, {})
        return stopped

    def forget(self, name: str) -> bool:
        name = _validate_name(name)
        row = self.db.get_managed_session(name)
        if row is None:
            return False
        record = WorkerRecord(
            name=str(row["name"]),
            runtime=str(row["runtime"]),
            transport=str(row["transport"]),
            device_id=str(row["device_id"]),
            project_id=str(row["project_id"]),
            cwd=str(row["cwd"]),
            command=str(row["command"]),
            status=str(row["status"]),
            exists=self.tmux.exists(name) if row["transport"] == "tmux" else False,
        )
        deleted = self.db.delete_managed_session(name)
        if deleted:
            self._audit("worker_forgotten", record, {})
        return deleted

    def _record(
        self,
        *,
        name: str,
        runtime: str,
        cwd: Path,
        command: str,
        status: str,
    ) -> WorkerRecord:
        project_id = project_id_from_cwd(str(cwd))
        self.db.upsert_managed_session(
            name=name,
            runtime=runtime,
            transport="tmux",
            device_id=self.settings.device_id,
            project_id=project_id,
            cwd=str(cwd),
            command=command,
            status=status,
        )
        record = WorkerRecord(
            name=name,
            runtime=runtime,
            transport="tmux",
            device_id=self.settings.device_id,
            project_id=project_id,
            cwd=str(cwd),
            command=command,
            status=status,
            exists=True,
        )
        self._audit("worker_started" if status == "running" else "worker_adopted", record, {})
        return record

    def _require_record(self, name: str) -> WorkerRecord:
        name = _validate_name(name)
        row = self.db.get_managed_session(name)
        if row is None:
            raise WorkerError(f"worker is not registered: {name}")
        if row["transport"] != "tmux":
            raise WorkerError(f"unsupported worker transport: {row['transport']}")
        if not self.tmux.exists(name):
            self.db.set_managed_session_status(name, status="missing")
            raise WorkerError(f"tmux session is missing: {name}")
        return WorkerRecord(
            name=str(row["name"]),
            runtime=str(row["runtime"]),
            transport=str(row["transport"]),
            device_id=str(row["device_id"]),
            project_id=str(row["project_id"]),
            cwd=str(row["cwd"]),
            command=str(row["command"]),
            status=str(row["status"]),
            exists=True,
        )

    def _audit(self, event_type: str, record: WorkerRecord, payload: dict) -> None:
        self.db.record_audit_event(
            category="worker",
            event_type=event_type,
            actor="worker-controller",
            device_id=self.settings.device_id,
            project_id=record.project_id,
            payload_json=json.dumps({**record.to_dict(), **payload}, sort_keys=True),
        )


def render_workers(records: list[WorkerRecord]) -> str:
    if not records:
        return "No managed workers registered.\n"
    lines = ["Managed workers:"]
    for record in records:
        exists = "live" if record.exists else "not-live"
        lines.append(
            f"- {record.name} [{record.runtime}/{record.transport}] {record.status} {exists} `{record.cwd}`"
        )
    return "\n".join(lines) + "\n"


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise WorkerError("worker name is required")
    if not WORKER_NAME_RE.match(cleaned):
        raise WorkerError("worker name may contain only letters, numbers, dot, colon, underscore, and dash")
    return cleaned


def _normalize_runtime(runtime: str) -> str:
    cleaned = runtime.strip().lower() or "shell"
    if cleaned == "bash":
        return "shell"
    return cleaned


def _command_in_cwd(cwd: Path, command: str) -> str:
    return f"cd {shlex.quote(str(cwd))} && exec {command}"
