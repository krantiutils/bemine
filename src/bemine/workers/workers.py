from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from ..config import BemineSettings, config_string
from ..db import BemineDB
from .tmux_adapter import TmuxAdapter
from ..util import project_id_from_cwd


WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
DEFAULT_RUNTIME_COMMANDS = {
    "shell": "bash",
    "codex": "codex --yolo",
    "claude": "claude --dangerously-skip-permissions",
    "kimi": "kimi",
}
REMOTE_TMUX_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"


class TmuxLike(Protocol):
    def start(self, name: str, command: str = "bash") -> object: ...

    def exists(self, name: str) -> bool: ...

    def send(self, name: str, text: str, *, enter: bool = True) -> None: ...

    def capture(self, name: str) -> str: ...

    def kill(self, name: str) -> None: ...


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
    runtime_session_id: str = ""

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
            "runtime_session_id": self.runtime_session_id,
        }


@dataclass(frozen=True)
class WorkerTransferResult:
    source: WorkerRecord
    target: WorkerRecord
    prompt: str
    duplicate_heads: list[WorkerRecord]

    def to_dict(self) -> dict:
        return {
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "prompt": self.prompt,
            "duplicate_heads": [record.to_dict() for record in self.duplicate_heads],
        }


@dataclass(frozen=True)
class WorkerDuplicateGroup:
    project_id: str
    heads: list[WorkerRecord]

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "heads": [record.to_dict() for record in self.heads],
        }


class WorkerError(RuntimeError):
    pass


class SshTmuxAdapter:
    def __init__(self, host: str, *, timeout: int = 8) -> None:
        host = host.strip()
        if not host:
            raise WorkerError("remote host is required")
        self.host = host
        self.timeout = timeout

    def start(self, name: str, command: str = "bash") -> object:
        self._run(["tmux", "new-session", "-d", "-s", _validate_name(name), command])
        return object()

    def exists(self, name: str) -> bool:
        proc = self._run(
            ["tmux", "has-session", "-t", _validate_name(name)],
            check=False,
        )
        return proc.returncode == 0

    def send(self, name: str, text: str, *, enter: bool = True) -> None:
        name = _validate_name(name)
        self._run(["tmux", "load-buffer", "-"], input=text)
        self._run(["tmux", "paste-buffer", "-t", name])
        if enter:
            self._run(["tmux", "send-keys", "-t", name, "Enter"])

    def capture(self, name: str) -> str:
        return self._run(
            ["tmux", "capture-pane", "-p", "-S", "-", "-t", _validate_name(name)]
        ).stdout

    def kill(self, name: str) -> None:
        self._run(
            ["tmux", "kill-session", "-t", _validate_name(name)],
            check=False,
        )

    def _run(
        self,
        args: list[str],
        *,
        input: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={min(self.timeout, 5)}",
                self.host,
                f"PATH={REMOTE_TMUX_PATH} {shlex.join(args)}",
            ],
            input=input,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or f"ssh exited {proc.returncode}"
            raise WorkerError(f"{self.host}: {detail}")
        return proc


class WorkerController:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        db: BemineDB | None = None,
        tmux: TmuxLike | None = None,
        remote_tmux_factory: Callable[[str], TmuxLike] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db or BemineDB(settings.db_path)
        self.tmux = tmux or TmuxAdapter()
        self.remote_tmux_factory = remote_tmux_factory or SshTmuxAdapter

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
        launch_command, runtime_session_id = _pin_runtime_session(runtime, command)
        launch_command = _launchable_command(runtime, launch_command)
        self.tmux.start(name, _command_in_cwd(cwd, launch_command))
        return self._record(
            name=name,
            runtime=runtime,
            cwd=cwd,
            command=command,
            status="running",
            runtime_session_id=runtime_session_id,
        )

    def start_on_device(
        self,
        *,
        name: str,
        runtime: str,
        cwd: Path,
        command: str = "",
        target_device: str = "",
    ) -> WorkerRecord:
        target_device = target_device.strip() or self.settings.device_id
        if self._is_local_device(target_device):
            return self.start(name=name, runtime=runtime, cwd=cwd, command=command)
        name = _validate_name(name)
        cwd = cwd.expanduser()
        runtime = _normalize_runtime(runtime)
        command = command.strip() or DEFAULT_RUNTIME_COMMANDS.get(runtime, runtime)
        target_tmux = self._tmux_for_device(target_device)
        if target_tmux.exists(name):
            raise WorkerError(f"tmux session already exists: {name}")
        target_tmux.start(name, _command_in_cwd(cwd, command))
        return self._record(
            name=name,
            runtime=runtime,
            cwd=cwd,
            command=command,
            status="running",
            transport=self._transport_for_device(target_device),
            device_id=target_device,
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
            record = self._row_to_record(row, exists=False)
            exists = self._record_exists(record)
            status = (
                "running"
                if exists and row["status"] in {"running", "adopted"}
                else row["status"]
            )
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
                    runtime_session_id=str(row["runtime_session_id"] or ""),
                )
            )
        return records

    def duplicate_heads(self) -> list[WorkerDuplicateGroup]:
        grouped: dict[str, list[WorkerRecord]] = {}
        for record in self.list():
            if record.status not in {"running", "adopted"}:
                continue
            grouped.setdefault(record.project_id, []).append(record)
        duplicates = [
            WorkerDuplicateGroup(project_id=project_id, heads=heads)
            for project_id, heads in grouped.items()
            if len(heads) > 1
        ]
        duplicates.sort(key=lambda group: (-len(group.heads), group.project_id))
        return duplicates

    def send(self, name: str, text: str, *, enter: bool = True) -> WorkerRecord:
        record = self._require_record(name)
        self._tmux_for_record(record).send(record.name, text, enter=enter)
        self.db.set_managed_session_status(record.name, status="running")
        self._audit("worker_sent", record, {"enter": enter, "text_len": len(text)})
        return record

    def send_when_ready(
        self,
        name: str,
        text: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> WorkerRecord:
        """Send to a CLI worker after it boots, answering startup prompts.

        Text sent right after start races the TUI boot (and trust prompts eat
        it entirely). Non-CLI workers get a plain immediate send."""
        record = self._require_record(name)
        runtime_key = record.runtime.strip().lower()
        if (
            runtime_key in {"claude", "codex", "kimi"}
            and _command_binary(record.command) == runtime_key
        ):
            send_to_cli_when_ready(
                self._tmux_for_record(record), record.name, text, sleep=sleep
            )
            self.db.set_managed_session_status(record.name, status="running")
            self._audit("worker_sent", record, {"enter": True, "text_len": len(text)})
            return record
        return self.send(name, text)

    def capture(self, name: str) -> str:
        record = self._require_record(name)
        captured = self._tmux_for_record(record).capture(record.name)
        self.db.set_managed_session_status(record.name, status="running")
        self._audit("worker_captured", record, {"chars": len(captured)})
        return captured

    def stop(self, name: str) -> WorkerRecord:
        record = self._require_record(name)
        self._tmux_for_record(record).kill(record.name)
        self.db.set_managed_session_status(record.name, status="stopped")
        stopped = WorkerRecord(
            **{**record.to_dict(), "status": "stopped", "exists": False}
        )
        self._audit("worker_stopped", stopped, {})
        return stopped

    def transfer(
        self,
        *,
        source_name: str,
        target_name: str,
        target_runtime: str,
        cwd: Path | None = None,
        command: str = "",
        target_device: str = "",
        stop_source: bool = False,
    ) -> WorkerTransferResult:
        source = self._require_record(source_name)
        target_name = _validate_name(target_name)
        target_cwd = cwd.expanduser().resolve() if cwd else Path(source.cwd)
        target_runtime = _normalize_runtime(target_runtime)
        target_device = target_device.strip() or self.settings.device_id
        target_tmux = self._tmux_for_device(target_device)
        target_transport = self._transport_for_device(target_device)
        source_capture = self._tmux_for_record(source).capture(source.name)
        if target_tmux.exists(target_name):
            target = self._record(
                name=target_name,
                runtime=target_runtime,
                cwd=target_cwd,
                command=command.strip() or "adopted-existing-tmux-session",
                status="adopted",
                transport=target_transport,
                device_id=target_device,
            )
        else:
            command = command.strip() or DEFAULT_RUNTIME_COMMANDS.get(
                target_runtime,
                target_runtime,
            )
            target_tmux.start(
                name=target_name,
                command=_command_in_cwd(target_cwd, command),
            )
            target = self._record(
                name=target_name,
                runtime=target_runtime,
                cwd=target_cwd,
                command=command,
                status="running",
                transport=target_transport,
                device_id=target_device,
            )
        duplicate_heads = self._active_project_heads(
            source.project_id,
            exclude_names={source.name, target.name},
        )
        prompt = _transfer_prompt(
            source=source,
            target=target,
            target_device=target_device,
            source_capture=source_capture,
            duplicate_heads=duplicate_heads,
        )
        target_tmux.send(target.name, prompt)
        if stop_source:
            source = self.stop(source.name)
        result = WorkerTransferResult(
            source=source,
            target=target,
            prompt=prompt,
            duplicate_heads=duplicate_heads,
        )
        self.db.record_audit_event(
            category="worker",
            event_type="worker_transfer",
            actor="worker-controller",
            device_id=self.settings.device_id,
            project_id=source.project_id,
            payload_json=json.dumps(
                {
                    "source": source.to_dict(),
                    "target": target.to_dict(),
                    "target_device": target_device,
                    "target_runtime": target_runtime,
                    "stop_source": stop_source,
                    "duplicate_heads": [record.to_dict() for record in duplicate_heads],
                    "conflict_policy": "preserve_all_heads_with_explicit_handoff",
                    "prompt_len": len(prompt),
                    "source_capture_len": len(source_capture),
                },
                sort_keys=True,
            ),
        )
        return result

    def forget(self, name: str) -> bool:
        name = _validate_name(name)
        row = self.db.get_managed_session(name)
        if row is None:
            return False
        record = self._row_to_record(row, exists=False)
        record = WorkerRecord(
            **{**record.to_dict(), "exists": self._record_exists(record)}
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
        transport: str = "tmux",
        device_id: str = "",
        runtime_session_id: str = "",
    ) -> WorkerRecord:
        project_id = project_id_from_cwd(str(cwd))
        device_id = device_id or self.settings.device_id
        self.db.upsert_managed_session(
            name=name,
            runtime=runtime,
            transport=transport,
            device_id=device_id,
            project_id=project_id,
            cwd=str(cwd),
            command=command,
            status=status,
            runtime_session_id=runtime_session_id,
        )
        record = WorkerRecord(
            name=name,
            runtime=runtime,
            transport=transport,
            device_id=device_id,
            project_id=project_id,
            cwd=str(cwd),
            command=command,
            status=status,
            exists=True,
            runtime_session_id=runtime_session_id,
        )
        event_type = "worker_started" if status == "running" else "worker_adopted"
        self._audit(event_type, record, {})
        return record

    def _require_record(self, name: str) -> WorkerRecord:
        name = _validate_name(name)
        row = self.db.get_managed_session(name)
        if row is None:
            raise WorkerError(f"worker is not registered: {name}")
        record = self._row_to_record(row, exists=True)
        if not self._record_exists(record):
            self.db.set_managed_session_status(name, status="missing")
            raise WorkerError(f"{row['transport']} session is missing: {name}")
        return record

    def _row_to_record(self, row: object, *, exists: bool) -> WorkerRecord:
        return WorkerRecord(
            name=str(row["name"]),
            runtime=str(row["runtime"]),
            transport=str(row["transport"]),
            device_id=str(row["device_id"]),
            project_id=str(row["project_id"]),
            cwd=str(row["cwd"]),
            command=str(row["command"]),
            status=str(row["status"]),
            exists=exists,
            runtime_session_id=str(row["runtime_session_id"] or ""),
        )

    def _record_exists(self, record: WorkerRecord) -> bool:
        try:
            return self._tmux_for_record(record).exists(record.name)
        except Exception:
            return False

    def _tmux_for_record(self, record: WorkerRecord) -> TmuxLike:
        if record.transport == "tmux":
            return self.tmux
        if record.transport == "ssh-tmux":
            return self.remote_tmux_factory(record.device_id)
        raise WorkerError(f"unsupported worker transport: {record.transport}")

    def _tmux_for_device(self, device_id: str) -> TmuxLike:
        if self._is_local_device(device_id):
            return self.tmux
        return self.remote_tmux_factory(device_id)

    def _transport_for_device(self, device_id: str) -> str:
        return "tmux" if self._is_local_device(device_id) else "ssh-tmux"

    def _is_local_device(self, device_id: str) -> bool:
        return device_id in {"", "local", self.settings.device_id}

    def _active_project_heads(
        self,
        project_id: str,
        *,
        exclude_names: set[str] | None = None,
    ) -> list[WorkerRecord]:
        exclude_names = exclude_names or set()
        heads: list[WorkerRecord] = []
        for record in self.list():
            if record.project_id != project_id:
                continue
            if record.name in exclude_names:
                continue
            if record.status not in {"running", "adopted"}:
                continue
            heads.append(record)
        return heads

    def _audit(self, event_type: str, record: WorkerRecord, payload: dict) -> None:
        self.db.record_audit_event(
            category="worker",
            event_type=event_type,
            actor="worker-controller",
            device_id=self.settings.device_id,
            project_id=record.project_id,
            payload_json=json.dumps({**record.to_dict(), **payload}, sort_keys=True),
        )


RESUME_NUDGE = (
    "Bemine restored this session after a reboot/line cut. "
    "Review the conversation above and continue from where you left off."
)
# Pre-start slack when matching indexed sessions to a worker: ingestion
# timestamps and worker created_at come from different clocks.
SESSION_LINK_SLACK_SECS = 120.0


def _command_binary(command: str) -> str:
    parts = command.split()
    return Path(parts[0]).name if parts else ""


# install dirs the systemd user PATH typically lacks
RUNTIME_BIN_PROBE_DIRS = (".local/bin", ".npm-global/bin", "bin")


def _resolve_runtime_bin(runtime_key: str) -> str:
    configured = config_string(runtime_key, "bin", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
    found = shutil.which(runtime_key)
    if found:
        return found
    for rel in RUNTIME_BIN_PROBE_DIRS:
        candidate = Path.home() / rel / runtime_key
        if candidate.is_file():
            return str(candidate)
    return runtime_key


def _launchable_command(runtime_key: str, command: str) -> str:
    """Swap a bare runtime CLI name for its absolute path at launch time.

    The daemon runs under systemd with a minimal PATH; after a reboot it is
    the first thing to start the tmux server, which then inherits that PATH —
    a bare `claude`/`codex`/`kimi` dies with "command not found". The DB keeps
    the bare command; resolution happens fresh on every launch."""
    parts = command.split()
    if not parts or parts[0] != runtime_key:
        return command
    resolved = _resolve_runtime_bin(runtime_key)
    if resolved == runtime_key:
        return command
    return " ".join([resolved, *parts[1:]])


def _pin_runtime_session(runtime: str, command: str) -> tuple[str, str]:
    """Pin a deterministic conversation id at launch where the CLI supports it.

    claude accepts --session-id <uuid> and --resume keeps that id (forking is
    opt-in via --fork-session). kimi's --session <uuid> creates-or-resumes, so
    a generated id is pinned the same way. codex has no launch-time id flag;
    its id is captured later by link_worker_runtime_sessions.
    Returns (launch_command, session_id)."""
    runtime_key = runtime.strip().lower()
    if _command_binary(command) != runtime_key:
        # custom command (e.g. `cat` in tests, wrapper scripts): the CLI flags
        # below would be meaningless or fatal for it
        return command, ""
    if runtime_key == "claude":
        if "--session-id" in command or "--resume" in command:
            return command, ""
        session_id = str(uuid.uuid4())
        return f"{command} --session-id {session_id}", session_id
    if runtime_key == "kimi":
        if any(flag in command for flag in ("--session", "--resume", "--continue")):
            return command, ""
        session_id = str(uuid.uuid4())
        return f"{command} --session {session_id}", session_id
    return command, ""


def _is_resumable_session_id(raw_id: str) -> bool:
    # stable_id fallbacks (claude_*/codex_*) are index-internal, not CLI-resumable
    return bool(raw_id) and not raw_id.startswith(("claude_", "codex_"))


def _runtime_session_on_disk(
    settings: BemineSettings, runtime_key: str, session_id: str
) -> bool:
    """True when the runtime still has this conversation on disk.

    Both CLIs exit 1 on resume with an unknown id, which would crash-loop the
    continuity tick — never resume an id whose transcript is gone."""
    if not session_id:
        return False
    try:
        if runtime_key == "claude":
            projects = settings.claude_home / "projects"
            return any(projects.glob(f"*/{session_id}.jsonl"))
        if runtime_key == "codex":
            sessions = settings.codex_home / "sessions"
            return any(sessions.glob(f"*/*/*/rollout-*{session_id}.jsonl"))
    except OSError:
        return False
    return False


def _settle_pane(
    tmux: TmuxLike,
    name: str,
    *,
    sleep: Callable[[float], None],
    settle_polls: int,
    poll_secs: float,
) -> str | None:
    """Wait until the pane stops changing; None when the session is gone."""
    pane = ""
    previous = None
    for _ in range(settle_polls):
        sleep(poll_secs)
        try:
            pane = tmux.capture(name)
        except Exception:
            return None
        if pane.strip() and pane == previous:
            return pane
        previous = pane
    return pane


def send_to_cli_when_ready(
    tmux: TmuxLike,
    name: str,
    text: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    settle_polls: int = 15,
    poll_secs: float = 2.0,
) -> None:
    """Deliver text to a CLI worker, tolerating boot time and startup prompts.

    Keys sent while the TUI is still booting get dropped (codex) or left
    unsubmitted in the input box (claude). Wait until the pane stops changing,
    answer known startup prompts, then send text and Enter separately; the
    trailing second Enter is a no-op when the first landed (submitting an
    empty input does nothing)."""
    pane = _settle_pane(
        tmux, name, sleep=sleep, settle_polls=settle_polls, poll_secs=poll_secs
    )
    if pane is None:
        return
    for _ in range(2):
        if "uv tool upgrade kimi-cli" in pane:
            # kimi's upgrade reminder maps Enter to "Upgrade now" — a blind
            # Enter would kick off the upgrade; `s` skips this version
            tmux.send(name, "s", enter=False)
        elif "trust this folder" in pane or "Do you trust" in pane:
            # claude/codex first-run trust prompt for a Bemine-managed cwd;
            # Enter selects the preselected "Yes, I trust / Yes, continue"
            tmux.send(name, "", enter=True)
        else:
            break
        pane = _settle_pane(
            tmux, name, sleep=sleep, settle_polls=settle_polls, poll_secs=poll_secs
        )
        if pane is None:
            return
    tmux.send(name, text, enter=False)
    sleep(1.0)
    tmux.send(name, "", enter=True)
    sleep(2.0)
    tmux.send(name, "", enter=True)


def _send_resume_nudge(
    tmux: TmuxLike,
    name: str,
    *,
    sleep: Callable[[float], None],
) -> None:
    send_to_cli_when_ready(tmux, name, RESUME_NUDGE, sleep=sleep)


def _native_resume_command(command: str, runtime_key: str, session_id: str) -> str:
    if runtime_key == "claude":
        return f"{command} --resume {session_id}"
    if runtime_key == "kimi":
        return f"{command} --session {session_id}"
    binary = command.split()[0] if command.split() else "codex"
    return f"{binary} resume {session_id} --dangerously-bypass-approvals-and-sandbox"


def _iso_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _resume_launch_command(
    db: BemineDB,
    settings: BemineSettings,
    *,
    runtime: str,
    cwd: str,
    command: str,
) -> tuple[str, str] | None:
    """Resolve a runtime-native resume launch for a worker's last conversation.

    Legacy heuristic for workers without a captured runtime_session_id: returns
    (launch_command, runtime_session_id) from the latest indexed session for
    this cwd, else None (caller starts fresh)."""
    runtime_key = runtime.strip().lower()
    if runtime_key not in {"claude", "codex"}:
        return None
    row = db.latest_session_for_cwd(
        runtime=runtime_key, device_id=settings.device_id, cwd=cwd
    )
    if row is None:
        return None
    raw_id = str(row["session_id"] or "").removeprefix(f"{runtime_key}:")
    if not _is_resumable_session_id(raw_id):
        return None
    source_path = str(row["source_path"] or "")
    if not source_path or not Path(source_path).exists():
        return None
    return _native_resume_command(command, runtime_key, raw_id), raw_id


def link_worker_runtime_sessions(
    settings: BemineSettings,
    *,
    db: BemineDB | None = None,
) -> list[tuple[str, str]]:
    """Capture conversation ids for running local workers that lack one.

    claude workers get their id pinned at launch; this pass covers codex (no
    launch-time id flag) and pre-capture workers. Only sessions whose
    started_at falls after the worker's created_at qualify, so a manual
    session in the same cwd that predates the worker can never be claimed."""
    db = db or BemineDB(settings.db_path)
    linked: list[tuple[str, str]] = []
    for row in db.list_managed_sessions():
        if str(row["runtime_session_id"] or ""):
            continue
        runtime_key = str(row["runtime"]).strip().lower()
        if runtime_key not in {"claude", "codex"}:
            continue
        if str(row["transport"]) != "tmux":
            continue
        if str(row["device_id"]) not in {settings.device_id, "local"}:
            continue
        if str(row["status"]) not in {"running", "adopted"}:
            continue
        started_after = float(row["created_at"] or 0) - SESSION_LINK_SLACK_SECS
        for session in db.sessions_for_cwd(
            runtime=runtime_key, device_id=settings.device_id, cwd=str(row["cwd"])
        ):
            if _iso_epoch(str(session["started_at"] or "")) < started_after:
                continue
            raw_id = str(session["session_id"] or "").removeprefix(f"{runtime_key}:")
            if not _is_resumable_session_id(raw_id):
                continue
            name = str(row["name"])
            db.set_worker_runtime_session(name, raw_id)
            db.record_audit_event(
                category="worker",
                event_type="worker_session_linked",
                actor="worker-controller",
                device_id=settings.device_id,
                project_id=str(row["project_id"]),
                payload_json=json.dumps(
                    {"name": name, "runtime_session_id": raw_id}, sort_keys=True
                ),
            )
            linked.append((name, raw_id))
            break
    return linked


def resume_missing_local_workers(
    settings: BemineSettings,
    *,
    db: BemineDB | None = None,
    tmux: TmuxLike | None = None,
    nudge_sleep: Callable[[float], None] = time.sleep,
) -> list[WorkerRecord]:
    """Restart Bemine-managed local tmux workers that were running before a reboot.

    This is intentionally limited to `status=running` local tmux sessions. Adopted
    sessions are opaque; Bemine cannot safely reconstruct their original command.
    When the sessions index knows the worker's last conversation for that cwd, the
    runtime is relaunched with its native resume (claude --resume / codex resume)
    so the agent keeps its context; otherwise it starts fresh."""
    db = db or BemineDB(settings.db_path)
    tmux = tmux or TmuxAdapter()
    controller = WorkerController(settings, db=db, tmux=tmux)
    resumed: list[WorkerRecord] = []
    for row in db.list_managed_sessions():
        record = controller._row_to_record(row, exists=False)
        if record.status not in {"running", "missing"}:
            continue
        if record.transport != "tmux":
            continue
        if record.device_id not in {settings.device_id, "local"}:
            continue
        if tmux.exists(record.name):
            continue
        cwd = Path(record.cwd).expanduser()
        if not cwd.exists() or not cwd.is_dir():
            continue
        command = record.command.strip()
        if not command or command == "adopted-existing-tmux-session":
            continue
        runtime_key = record.runtime.strip().lower()
        launch_command = command
        resumed_session = ""
        # resume flags only make sense when the command really is the runtime
        # CLI — custom commands (wrappers, test stubs) restart as-is
        supports_native = _command_binary(command) == runtime_key
        if (
            supports_native
            and _is_resumable_session_id(record.runtime_session_id)
            and (
                # kimi --session creates-or-resumes, so unknown ids are safe;
                # the other CLIs exit 1 on an id with no transcript on disk
                runtime_key == "kimi"
                or _runtime_session_on_disk(
                    settings, runtime_key, record.runtime_session_id
                )
            )
        ):
            # deterministic path: the id pinned at start (claude/kimi) or
            # captured by the linking pass (codex); ids survive resumes
            resumed_session = record.runtime_session_id
            launch_command = _native_resume_command(
                command, runtime_key, resumed_session
            )
        elif supports_native:
            resume = _resume_launch_command(
                db,
                settings,
                runtime=record.runtime,
                cwd=str(cwd.resolve()),
                command=command,
            )
            if resume is not None:
                launch_command, resumed_session = resume
        session_id = resumed_session
        if not resumed_session:
            launch_command, session_id = _pin_runtime_session(runtime_key, command)
        launch_command = _launchable_command(runtime_key, launch_command)
        tmux.start(record.name, _command_in_cwd(cwd.resolve(), launch_command))
        if resumed_session:
            _send_resume_nudge(tmux, record.name, sleep=nudge_sleep)
        # record keeps the ORIGINAL command; the conversation id lives in
        # runtime_session_id and the resume flags are rebuilt per launch
        restarted = controller._record(
            name=record.name,
            runtime=record.runtime,
            cwd=cwd.resolve(),
            command=command,
            status="running",
            transport="tmux",
            device_id=settings.device_id,
            runtime_session_id=session_id,
        )
        controller._audit(
            "worker_resumed_after_outage",
            restarted,
            {
                "previous_device_id": record.device_id,
                "resumed_runtime_session": resumed_session,
            },
        )
        resumed.append(restarted)
    return resumed


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


def render_transfer(result: WorkerTransferResult) -> str:
    lines = [
        "Worker transfer queued:",
        f"- source: `{result.source.name}` [{result.source.runtime}] `{result.source.cwd}`",
        f"- target: `{result.target.name}` [{result.target.runtime}] `{result.target.cwd}`",
        f"- duplicate active heads: {len(result.duplicate_heads)}",
    ]
    if result.duplicate_heads:
        for record in result.duplicate_heads:
            lines.append(
                f"  - `{record.name}` [{record.runtime}] {record.status} `{record.cwd}`"
            )
    lines.append("")
    return "\n".join(lines)


def render_duplicate_heads(groups: list[WorkerDuplicateGroup]) -> str:
    if not groups:
        return "No duplicate active worker heads found.\n"
    lines = ["Duplicate active worker heads:"]
    for group in groups:
        lines.append(f"- project `{group.project_id}` heads={len(group.heads)}")
        for record in group.heads:
            lines.append(
                f"  - `{record.name}` [{record.runtime}/{record.transport}] "
                f"{record.status} device={record.device_id} cwd=`{record.cwd}`"
            )
    return "\n".join(lines) + "\n"


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise WorkerError("worker name is required")
    if not WORKER_NAME_RE.match(cleaned):
        raise WorkerError(
            "worker name may contain only letters, numbers, dot, colon, "
            "underscore, and dash"
        )
    return cleaned


def _normalize_runtime(runtime: str) -> str:
    cleaned = runtime.strip().lower() or "shell"
    if cleaned == "bash":
        return "shell"
    return cleaned


def _command_in_cwd(cwd: Path, command: str) -> str:
    return f"cd {shlex.quote(str(cwd))} && exec {command}"


def _transfer_prompt(
    *,
    source: WorkerRecord,
    target: WorkerRecord,
    target_device: str,
    source_capture: str,
    duplicate_heads: list[WorkerRecord],
) -> str:
    duplicate_lines = [
        f"- {record.name} [{record.runtime}] {record.status} device={record.device_id} cwd={record.cwd}"
        for record in duplicate_heads
    ] or ["- none"]
    return "\n".join(
        [
            "Bemine worker transfer handoff.",
            "",
            "You are receiving project context from another managed worker.",
            f"Source worker: {source.name}",
            f"Source runtime: {source.runtime}",
            f"Source device: {source.device_id}",
            f"Target worker: {target.name}",
            f"Target runtime: {target.runtime}",
            f"Target device: {target_device}",
            f"Project id: {source.project_id}",
            f"Project cwd: {source.cwd}",
            "",
            "Duplicate active project heads:",
            *duplicate_lines,
            "",
            "Conflict policy:",
            "- Do not assume your view is canonical.",
            "- Preserve source context and inspect repo state before overwriting work.",
            "- If duplicate heads conflict, summarize the conflict and ask for direction.",
            "",
            "Captured source pane:",
            "```text",
            source_capture.strip() or "(empty)",
            "```",
            "",
            "Start by acknowledging the transfer, summarizing current state, and listing the next safe action.",
        ]
    )
