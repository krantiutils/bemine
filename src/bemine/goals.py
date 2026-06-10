from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import BemineSettings, config_int, config_string
from .db import BemineDB
from .index.status import collect_fleet_status
from .util import compact_text, project_id_from_cwd, safe_filename
from .workers.workers import WorkerController, WorkerRecord, WorkerError


DEFAULT_GOAL_RUNTIME = "claude"
DEFAULT_GOAL_INTERVAL_SECS = 600


@dataclass(frozen=True)
class GoalStartResult:
    worker: WorkerRecord
    watcher: dict
    prompt: str

    def to_dict(self) -> dict:
        return {
            "worker": self.worker.to_dict(),
            "watcher": dict(self.watcher),
            "prompt": self.prompt,
        }


@dataclass(frozen=True)
class GoalTarget:
    cwd: Path
    device_id: str


@dataclass(frozen=True)
class GoalSession:
    project_id: str
    worker: WorkerRecord | None
    watcher: dict | None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "worker": self.worker.to_dict() if self.worker else None,
            "watcher": dict(self.watcher) if self.watcher else None,
        }


def start_goal(
    settings: BemineSettings,
    *,
    project: str,
    brief: str,
    cwd: Path | None = None,
    runtime: str = "",
    worker_name: str = "",
    interval_secs: int = 0,
    channel_id: str = "",
    command: str = "",
    target_device: str = "",
    db: BemineDB | None = None,
    controller: WorkerController | None = None,
) -> GoalStartResult:
    project = project.strip()
    brief = brief.strip()
    if not project:
        raise ValueError("project is required")
    if not brief:
        raise ValueError("goal brief is required")
    db = db or BemineDB(settings.db_path)
    target = _resolve_goal_target(
        settings,
        db,
        project=project,
        cwd=cwd,
        target_device=target_device,
    )
    project_cwd = target.cwd
    runtime = runtime.strip() or config_string("goal", "runtime", DEFAULT_GOAL_RUNTIME)
    interval_secs = interval_secs or config_int(
        "goal", "interval_secs", DEFAULT_GOAL_INTERVAL_SECS
    )
    controller = controller or WorkerController(settings, db=db)
    base_name = worker_name.strip() or _default_worker_name(project, project_cwd)
    worker = _start_unique_worker(
        controller,
        base_name=base_name,
        runtime=runtime,
        cwd=project_cwd,
        command=command,
        target_device=target.device_id,
    )
    prompt = _goal_prompt(project=project, brief=brief, worker=worker)
    controller.send(worker.name, prompt)
    project_id = worker.project_id or project_id_from_cwd(str(project_cwd))
    watcher_channel = channel_id.strip() or db.get_channel_id_for_project(project_id) or ""
    db.upsert_project_watcher(
        project_id=project_id,
        channel_id=watcher_channel,
        brief=brief,
        interval_secs=interval_secs,
    )
    watcher = db.get_project_watcher(project_id) or {}
    db.record_audit_event(
        category="goal",
        event_type="goal_started",
        actor="goal-command",
        device_id=settings.device_id,
        project_id=project_id,
        payload_json=json.dumps(
            {
                "project": project,
                "cwd": str(project_cwd),
                "target_device": target.device_id,
                "runtime": runtime,
                "worker": worker.to_dict(),
                "watcher": watcher,
                "brief": compact_text(brief, limit=500),
                "prompt_len": len(prompt),
            },
            sort_keys=True,
        ),
    )
    return GoalStartResult(worker=worker, watcher=watcher, prompt=prompt)


def list_goal_sessions(
    settings: BemineSettings,
    *,
    status: str = "",
    db: BemineDB | None = None,
    controller: WorkerController | None = None,
) -> list[GoalSession]:
    db = db or BemineDB(settings.db_path)
    controller = controller or WorkerController(settings, db=db)
    workers = controller.list()
    watchers = {
        str(row["project_id"]): dict(row)
        for row in db.list_active_project_watchers()
    }
    sessions: list[GoalSession] = []
    seen_projects: set[str] = set()
    for worker in workers:
        watcher = watchers.get(worker.project_id)
        if not watcher and not worker.name.startswith("goal-"):
            continue
        sessions.append(
            GoalSession(
                project_id=worker.project_id,
                worker=worker,
                watcher=watcher,
            )
        )
        seen_projects.add(worker.project_id)
    for project_id, watcher in watchers.items():
        if project_id in seen_projects:
            continue
        sessions.append(GoalSession(project_id=project_id, worker=None, watcher=watcher))
    query = status.strip().casefold()
    if query:
        sessions = [
            session
            for session in sessions
            if _goal_session_matches(session, query)
        ]
    sessions.sort(key=_goal_session_sort_key)
    return sessions


def render_goal_start(result: GoalStartResult) -> str:
    watcher = result.watcher
    return (
        "Goal session started:\n"
        f"- worker: `{result.worker.name}` [{result.worker.runtime}] "
        f"device={result.worker.device_id} `{result.worker.cwd}`\n"
        f"- watcher: `{watcher.get('project_id', result.worker.project_id)}` "
        f"every {watcher.get('interval_secs', '?')}s "
        f"channel={watcher.get('channel_id') or 'none'}\n"
    )


def render_goal_sessions(sessions: list[GoalSession]) -> str:
    if not sessions:
        return "No goal sessions found.\n"
    lines = ["Goal sessions:"]
    for session in sessions:
        lines.append(f"- project `{session.project_id}`")
        if session.worker:
            live = "live" if session.worker.exists else "not-live"
            lines.append(
                f"  worker: `{session.worker.name}` [{session.worker.runtime}] "
                f"{session.worker.status}/{live} device={session.worker.device_id} "
                f"`{session.worker.cwd}`"
            )
        else:
            lines.append("  worker: none registered")
        if session.watcher:
            lines.append(
                f"  watcher: {session.watcher.get('status', 'unknown')} "
                f"every {session.watcher.get('interval_secs', '?')}s "
                f"channel={session.watcher.get('channel_id') or 'none'}"
            )
        else:
            lines.append("  watcher: none active")
    lines.append("")
    return "\n".join(lines)


def _resolve_goal_target(
    settings: BemineSettings,
    db: BemineDB,
    *,
    project: str,
    cwd: Path | None,
    target_device: str = "",
) -> GoalTarget:
    requested_device = target_device.strip()
    if cwd is not None:
        device_id = requested_device or settings.device_id
        if device_id in {"", "local", settings.device_id}:
            return GoalTarget(
                cwd=_require_directory(cwd.expanduser().resolve(), label="--cwd"),
                device_id=settings.device_id,
            )
        return GoalTarget(cwd=cwd.expanduser(), device_id=device_id)
    candidate = Path(project).expanduser()
    if candidate.exists():
        return GoalTarget(
            cwd=_require_directory(candidate.resolve(), label=project),
            device_id=requested_device or settings.device_id,
        )
    normalized = project.casefold()
    for row in db.list_projects():
        row_project_id = str(row["project_id"])
        row_name = str(row["name"])
        row_cwd = str(row["cwd"])
        if normalized in {
            row_project_id.casefold(),
            row_name.casefold(),
            Path(row_cwd).name.casefold(),
        }:
            return GoalTarget(
                cwd=_require_directory(
                    Path(row_cwd).expanduser().resolve(),
                    label=f"indexed project {project!r}",
                ),
                device_id=requested_device or settings.device_id,
            )
    fleet_target = _resolve_goal_target_from_fleet(
        settings,
        project=project,
        target_device=requested_device,
    )
    if fleet_target is not None:
        return fleet_target
    raise ValueError(
        f"unknown project {project!r}; run `bemine scan` or pass --cwd /path/to/project"
    )


def _require_directory(path: Path, *, label: str) -> Path:
    if not path.exists():
        raise ValueError(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")
    return path


def _default_worker_name(project: str, cwd: Path) -> str:
    label = project or cwd.name
    return "goal-" + safe_filename(label, fallback=cwd.name or "project").lower()[:48]


def _start_unique_worker(
    controller: WorkerController,
    *,
    base_name: str,
    runtime: str,
    cwd: Path,
    command: str,
    target_device: str,
) -> WorkerRecord:
    names = [base_name, *(f"{base_name}-{index}" for index in range(2, 100))]
    last_error: Exception | None = None
    for name in names:
        try:
            return controller.start_on_device(
                name=name,
                runtime=runtime,
                cwd=cwd,
                command=command,
                target_device=target_device,
            )
        except WorkerError as exc:
            if "already exists" not in str(exc):
                raise
            last_error = exc
            continue
    raise WorkerError(f"could not allocate worker name from {base_name!r}") from last_error


def _resolve_goal_target_from_fleet(
    settings: BemineSettings,
    *,
    project: str,
    target_device: str = "",
) -> GoalTarget | None:
    try:
        statuses, _errors = collect_fleet_status(
            settings,
            devices=[target_device] if target_device else None,
            limit_per_runtime=20,
            timeout=6,
        )
    except Exception:
        return None
    normalized = project.casefold()
    matches = [
        status
        for status in statuses
        if _project_matches_cwd(normalized, status.cwd)
        and (not target_device or status.device_id == target_device)
    ]
    if not matches:
        return None
    matches.sort(key=lambda status: float(status.last_interaction_at or 0), reverse=True)
    chosen = matches[0]
    return GoalTarget(cwd=Path(chosen.cwd).expanduser(), device_id=chosen.device_id)


def _project_matches_cwd(project: str, cwd: str) -> bool:
    if not cwd:
        return False
    path = Path(cwd)
    variants = {
        path.name.casefold(),
        str(path).casefold(),
    }
    return project in variants


def _goal_session_matches(session: GoalSession, query: str) -> bool:
    values = [session.project_id]
    if session.worker:
        values.extend(
            [
                session.worker.name,
                session.worker.cwd,
                Path(session.worker.cwd).name,
                session.worker.device_id,
                session.worker.runtime,
            ]
        )
    if session.watcher:
        values.extend(
            [
                str(session.watcher.get("channel_id") or ""),
                str(session.watcher.get("brief") or ""),
                str(session.watcher.get("status") or ""),
            ]
        )
    return any(query in value.casefold() for value in values if value)


def _goal_session_sort_key(session: GoalSession) -> tuple[int, str]:
    tick = 0
    if session.watcher:
        tick = int(float(session.watcher.get("last_tick_at") or 0))
    return (tick, session.project_id)


def _goal_prompt(*, project: str, brief: str, worker: WorkerRecord) -> str:
    return "\n".join(
        [
            "# Bemine Goal Session",
            "",
            f"Project: {project}",
            f"Project root: {worker.cwd}",
            f"Runtime: {worker.runtime}",
            "",
            "Goal:",
            brief,
            "",
            "Operate autonomously toward this goal.",
            "- Read the repo and Bemine context before changing behavior.",
            "- Run commands yourself; do not hand commands back to the user.",
            "- Keep durable findings in the repo, Bemine state, or the Obsidian vault as appropriate.",
            "- Use tests, benchmarks, or concrete artifacts as evidence before claiming completion.",
            "- If blocked on credentials, destructive approval, or missing external state, say exactly what is blocked.",
            "- End substantial progress with `uv run bemine handoff \"<summary>\" --next \"<next>\" --test \"<verification>\"`.",
        ]
    )
