from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import BemineSettings, config_bool, config_string
from ..db import BemineDB
from ..index.status import _redact, collect_fleet_status


ToolHandler = Callable[[Mapping[str, Any], "ToolContext"], Mapping[str, Any]]


@dataclass(frozen=True)
class ToolContext:
    settings: BemineSettings
    db: BemineDB
    session_id: str
    project_id: str
    cwd: Path


@dataclass(frozen=True)
class BemineTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    policy: str = "auto"

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


class ToolExecutionError(RuntimeError):
    pass


class BemineToolRegistry:
    def __init__(self, tools: list[BemineTool] | None = None) -> None:
        self._tools = {tool.name: tool for tool in (tools or default_tools())}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_anthropic() for tool in self._tools.values()]

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai() for tool in self._tools.values()]

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> Mapping[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(f"unknown tool: {name}")
        return tool.handler(arguments, context)


def default_tools() -> list[BemineTool]:
    return [
        BemineTool(
            name="status_show",
            description="Show recent Codex, Claude, and Kimi sessions by last interaction.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            },
            handler=_status_show,
        ),
        BemineTool(
            name="bemine_config",
            description=(
                "Show Bemine's active runtime, model, device, and configured defaults. "
                "AUTHORITATIVE — use this to answer any question about which model/runtime/"
                "configuration is in use; never guess."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_bemine_config,
        ),
        BemineTool(
            name="jobs_list",
            description="List Bemine's scheduled cron jobs (name, schedule, enabled, last run).",
            input_schema={"type": "object", "properties": {}},
            handler=_jobs_list,
        ),
        BemineTool(
            name="usage_report",
            description="Show recorded token usage by provider (codex/claude/kimi).",
            input_schema={
                "type": "object",
                "properties": {"provider": {"type": "string"}},
            },
            handler=_usage_report,
        ),
        BemineTool(
            name="audit_list",
            description="List recent Bemine audit events, optionally filtered by category or event type.",
            input_schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "event_type": {"type": "string"},
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
            handler=_audit_list,
        ),
        BemineTool(
            name="shell_run",
            description=(
                "Run a shell command. Use this for any task other tools don't cover — "
                "system administration, installing tools, managing services, and calling "
                "external APIs (e.g. Discord via $MAYOR_DISCORD_BOT_TOKEN). Scope depends "
                "on [policy].shell: project-only by default, or the whole machine when "
                "unrestricted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
            },
            handler=_shell_run,
            policy="project-write",
        ),
        BemineTool(
            name="tmux_list",
            description=(
                "List running tmux panes (target, current path, current command). Use this "
                "to find which session a user's reply should go to — match by project path."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_tmux_list,
        ),
        BemineTool(
            name="tmux_send",
            description=(
                "Send input to a running tmux pane (e.g. relay the user's reply into a "
                "waiting codex/claude/kimi terminal session). Sends the text then Enter to "
                "submit, unless enter=false. Get the target from tmux_list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "tmux target, e.g. 'work:0.1' or a session name"},
                    "text": {"type": "string"},
                    "enter": {"type": "boolean"},
                },
                "required": ["target", "text"],
            },
            handler=_tmux_send,
            policy="project-write",
        ),
        BemineTool(
            name="tmux_capture",
            description="Capture the recent visible output of a tmux pane (to see a session's current state).",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["target"],
            },
            handler=_tmux_capture,
        ),
        BemineTool(
            name="watch_control",
            description=(
                "Start, stop, or report a per-project WATCHER for THIS channel's project. "
                "A watcher polls the project's agent session and, when it is waiting on "
                "input, drives it toward the brief (sending keystrokes), escalating here "
                "only when blocked. Use action='start' with the user's full context as "
                "`brief` and `interval_secs` (convert phrases like 'every 10m' -> 600; "
                "default 300). Use action='stop' to disable, action='status' to report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop", "status"]},
                    "brief": {"type": "string"},
                    "interval_secs": {"type": "integer", "minimum": 30, "maximum": 86400},
                },
                "required": ["action"],
            },
            handler=_watch_control,
            policy="project-write",
        ),
        BemineTool(
            name="file_read",
            description="Read a UTF-8 text file inside the project root.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50000},
                },
                "required": ["path"],
            },
            handler=_file_read,
        ),
        BemineTool(
            name="file_search",
            description="Search text inside the project root with ripgrep.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "glob": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["query"],
            },
            handler=_file_search,
        ),
    ]


def _status_show(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    limit = _int(args.get("limit"), default=8, minimum=1, maximum=20)
    statuses, errors = collect_fleet_status(context.settings, limit_per_runtime=limit)
    return {
        "statuses": [status.to_dict() for status in statuses[:limit]],
        "errors": [error.__dict__ for error in errors],
    }


def _bemine_config(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    del args
    session = context.db.get_bemine_session(context.session_id) or {}
    return {
        "active_runtime": session.get("runtime") or "",
        "active_model": session.get("model") or "",
        "device": context.settings.device_id,
        "default_runtime": config_string("dispatch", "runtime", ""),
        "available_runtimes": {
            "kimi": config_string("kimi", "model", ""),
            "claude": f'{config_string("claude", "model", "")} (via Meridian proxy)',
            "codex": "ChatGPT plan via codex app-server",
        },
    }


def _jobs_list(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    del args
    rows = context.db.list_jobs()
    return {
        "jobs": [
            {
                "job_id": row["job_id"],
                "schedule": row["schedule"],
                "enabled": bool(row["enabled"]),
                "last_run_at": row["last_run_at"],
                "failure_count": row["failure_count"],
            }
            for row in rows
        ]
    }


def _usage_report(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    provider = str(args.get("provider") or "").strip() or None
    return {"providers": context.db.usage_rollup(provider=provider)}


def _audit_list(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    limit = _int(args.get("limit"), default=8, minimum=1, maximum=20)
    rows = context.db.list_audit_events(
        category=str(args.get("category") or "").strip(),
        event_type=str(args.get("event_type") or "").strip(),
        project_id=str(args.get("project_id") or "").strip(),
        limit=limit,
    )
    events = []
    for row in rows:
        payload = str(row["payload_json"] or "")
        try:
            parsed_payload: object = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed_payload = {"raw": payload}
        events.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "category": row["category"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "device_id": row["device_id"],
                "project_id": row["project_id"],
                "payload": parsed_payload,
            }
        )
    return {"events": events}


def _shell_run(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    policy = config_string("policy", "shell", "project-write")
    if policy == "disabled":
        raise ToolExecutionError("shell_run is disabled by policy")
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolExecutionError("command is required")
    if policy == "unrestricted":
        # Full-system shell: any working directory, no project sandbox. Enabled
        # deliberately via [policy].shell = "unrestricted".
        raw_cwd = str(args.get("cwd") or "").strip()
        cwd = (Path(raw_cwd).expanduser() if raw_cwd else context.cwd).resolve()
    else:
        cwd = _resolve_project_path(str(args.get("cwd") or "."), context.cwd)
    timeout = _int(args.get("timeout"), default=60, minimum=1, maximum=600)
    proc = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=_shell_env(cwd),
    )
    return {
        "command": command,
        "cwd": str(cwd),
        "returncode": proc.returncode,
        "stdout": _redact(proc.stdout[-12000:]),
        "stderr": _redact(proc.stderr[-12000:]),
    }


def _tmux_list(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    proc = subprocess.run(
        ["tmux", "list-panes", "-a", "-F",
         "#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_path}\t#{pane_current_command}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
    )
    panes = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            panes.append({"target": parts[0], "path": parts[1], "command": parts[2]})
    return {"panes": panes, "stderr": _redact(proc.stderr[-500:])}


def _tmux_send(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolExecutionError("target (tmux session/pane) is required")
    text = str(args.get("text") or "")
    enter = args.get("enter", True)
    enter = True if enter is None else bool(enter)
    # Send literal text first, then a separate Enter so the running program submits it.
    send = subprocess.run(["tmux", "send-keys", "-t", target, "-l", text],
                          text=True, stderr=subprocess.PIPE, timeout=15, check=False)
    if send.returncode != 0:
        raise ToolExecutionError(f"tmux send-keys failed: {send.stderr.strip()}")
    if enter:
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                       text=True, stderr=subprocess.PIPE, timeout=15, check=False)
    return {"target": target, "sent": _redact(text), "enter": enter, "ok": True}


def _tmux_capture(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolExecutionError("target (tmux session/pane) is required")
    lines = _int(args.get("lines"), default=40, minimum=1, maximum=200)
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", target, "-S", f"-{lines}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
    )
    if proc.returncode != 0:
        raise ToolExecutionError(f"tmux capture-pane failed: {proc.stderr.strip()}")
    return {"target": target, "output": _redact(proc.stdout[-6000:])}


def _watch_control(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    action = str(args.get("action") or "status")
    project_id = context.project_id
    session = context.db.get_bemine_session(context.session_id) or {}
    channel_id = str(session.get("channel_id") or "")
    project_name = project_id.split(":")[-1]

    if action == "start":
        brief = str(args.get("brief") or "").strip()
        if not brief:
            return {"ok": False, "error": "a context brief is required to start a watcher"}
        interval = int(args.get("interval_secs") or 300)
        context.db.upsert_project_watcher(
            project_id=project_id, channel_id=channel_id, brief=brief, interval_secs=interval,
        )
        return {"ok": True, "status": "active", "project": project_name,
                "interval_secs": interval}

    if action == "stop":
        context.db.update_project_watcher(project_id, status="stopped")
        return {"ok": True, "status": "stopped", "project": project_name}

    watcher = context.db.get_project_watcher(project_id)
    if not watcher:
        return {"ok": True, "status": "none", "project": project_name}
    return {"ok": True, "status": watcher["status"], "project": project_name,
            "interval_secs": watcher["interval_secs"],
            "awaiting_reply": bool(watcher["awaiting_reply"])}


def _file_read(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    path = _resolve_project_path(str(args.get("path") or ""), context.cwd)
    limit = _int(args.get("limit"), default=20000, minimum=1, maximum=50000)
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(path), "content": _redact(text[:limit]), "truncated": len(text) > limit}


def _file_search(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolExecutionError("query is required")
    command = ["rg", "--line-number", "--no-heading", "--color", "never"]
    glob = str(args.get("glob") or "").strip()
    if glob:
        command.extend(["--glob", glob])
    command.append(query)
    proc = subprocess.run(
        command,
        cwd=context.cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    limit = _int(args.get("limit"), default=80, minimum=1, maximum=200)
    lines = proc.stdout.splitlines()[:limit]
    return {
        "query": query,
        "cwd": str(context.cwd),
        "returncode": proc.returncode,
        "matches": [_redact(line) for line in lines],
        "stderr": _redact(proc.stderr[-2000:]),
    }


def _load_env_files(*extra: Path) -> dict[str, str]:
    """Parse the same .env files the Discord gateway reads (so the agent's shell has the
    user's credentials, e.g. DISCORD_BOT_TOKEN, KIMI_API_KEY). KEY=VALUE lines only."""
    loaded: dict[str, str] = {}
    paths = [Path.home() / ".hermes" / ".env", *extra]
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                loaded[key] = value.strip().strip('"').strip("'")
    return loaded


def _shell_env(cwd: Path) -> dict[str, str]:
    """Subprocess environment for shell_run: OS env wins, .env secrets fill the gaps.
    Aliases DISCORD_BOT_TOKEN -> MAYOR_DISCORD_BOT_TOKEN so either name works."""
    env = {**_load_env_files(cwd / ".env"), **os.environ}
    token = env.get("MAYOR_DISCORD_BOT_TOKEN") or env.get("DISCORD_BOT_TOKEN")
    if token:
        env.setdefault("DISCORD_BOT_TOKEN", token)
        env.setdefault("MAYOR_DISCORD_BOT_TOKEN", token)
    return env


def _resolve_project_path(value: str, project_root: Path) -> Path:
    if not value:
        raise ToolExecutionError("path is required")
    project_root = project_root.expanduser().resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    resolved = path.resolve()
    allow_external = config_bool("policy", "allow_external_paths", False)
    if not allow_external and not _is_relative_to(resolved, project_root):
        raise ToolExecutionError(f"path outside project root is not allowed: {resolved}")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
