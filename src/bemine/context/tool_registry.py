from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import BemineSettings, config_bool, config_string
from ..backlog import add_backlog_item, default_backlog_owner, list_backlog_items
from ..briefs import run_brief
from ..db import BemineDB
from ..discord.store import Store as DiscordStore
from ..discord.transcript import redact_message_rows
from ..goals import list_goal_sessions, start_goal
from ..health import collect_health
from ..index.status import _redact, collect_fleet_status
from ..workers.workers import WorkerError


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
            name="health_status",
            description=(
                "Return Bemine's deploy/services/jobs/watchers health packet. Use this "
                "before drilling into jobs, systemd, deploy, or watcher status."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_health_status,
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
            name="backlog_add",
            description=(
                "Add a durable PA/SMM backlog item to the Obsidian vault. Use when the "
                "user asks to store an idea, social post angle, follow-up, or PA task."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "owner": {"type": "string", "description": "PA or SMM"},
                },
                "required": ["text"],
            },
            handler=_backlog_add,
        ),
        BemineTool(
            name="backlog_list",
            description="List open PA/SMM backlog items from the Obsidian vault.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "PA or SMM"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
            handler=_backlog_list,
        ),
        BemineTool(
            name="brief_run",
            description=(
                "Generate the deterministic PA/SMM brief from recent sessions, Discord "
                "history, audit events, and tool state. Use when the user asks what to "
                "focus on, asks for PA/SMM nudges, or wants social post ideas."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "record_backlog": {
                        "type": "boolean",
                        "description": "Whether to append non-duplicate generated PA/SMM backlog items.",
                    },
                },
            },
            handler=_brief_run,
        ),
        BemineTool(
            name="goal_start",
            description=(
                "Start a managed Bemine goal session: create a worker, send the goal "
                "brief, and register a watcher that keeps driving it. Use this when the "
                "user asks Bemine to start/run a goal itself instead of merely watching "
                "an existing session."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "brief": {"type": "string", "description": "The concrete goal brief."},
                    "project": {
                        "type": "string",
                        "description": "Project name/id/path. Defaults to this channel's project when omitted.",
                    },
                    "cwd": {"type": "string", "description": "Explicit project root."},
                    "runtime": {"type": "string", "description": "Worker runtime override."},
                    "worker": {"type": "string", "description": "Worker/tmux session name."},
                    "interval_secs": {"type": "integer", "minimum": 30, "maximum": 86400},
                    "channel_id": {"type": "string", "description": "Discord channel id override."},
                    "command": {"type": "string", "description": "Worker command override."},
                    "target_device": {"type": "string", "description": "Remote device override."},
                },
                "required": ["brief"],
            },
            handler=_goal_start,
            policy="project-write",
        ),
        BemineTool(
            name="goal_status",
            description=(
                "List managed goal workers and active watchers, optionally filtered by "
                "project, worker, cwd, device, status, or brief text."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            handler=_goal_status,
        ),
        BemineTool(
            name="discord_threads",
            description=(
                "List locally stored Discord channels/threads by recent activity. Use "
                "this to discover relevant project/history channels before reading a "
                "transcript; searches local indexed Discord history only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search channel id/name/category/author/content.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
            handler=_discord_threads,
        ),
        BemineTool(
            name="discord_transcript",
            description=(
                "Read recent locally stored Discord transcript messages. Use query for "
                "global local search, or channel_id for an exact channel/thread id. "
                "Defaults to this session's Discord channel when both are omitted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": "Search channel id/name/category/author/content.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
            handler=_discord_transcript,
        ),
        BemineTool(
            name="channel_routes",
            description=(
                "Inspect Bemine's project-to-Discord routing map. Reports configured "
                "routes, indexed project route candidates, latest seen channel ids, and "
                "active watcher channel bindings without mutating Discord."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Filter by channel, project id/name, cwd, runtime, or channel id.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            handler=_channel_routes,
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
            name="deploy_control",
            description=(
                "Deploy Bemine's own current committed code (HEAD). action='request' runs "
                "the test-gated, auto-rollback deploy (restarts the gateway + daemon, reverts "
                "if unhealthy). Use this when the user says to deploy/ship Bemine. "
                "action='status' reports the current deploy phase. The working tree must be "
                "clean (changes committed) first."
            ),
            input_schema={
                "type": "object",
                "properties": {"action": {"type": "string", "enum": ["request", "status"]}},
                "required": ["action"],
            },
            handler=_deploy_control,
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


def _health_status(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    del args
    return collect_health(context.settings, db=context.db)


def _usage_report(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    provider = str(args.get("provider") or "").strip() or None
    return {"providers": context.db.usage_rollup(provider=provider)}


def _backlog_add(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolExecutionError("text is required")
    owner = str(args.get("owner") or "").strip() or default_backlog_owner()
    entry = add_backlog_item(
        context.settings,
        owner=owner,
        text=text,
        source=f"tool:{context.session_id}",
        project=context.project_id,
        db=context.db,
    )
    return {"ok": True, "entry": entry.to_dict()}


def _backlog_list(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    owner = str(args.get("owner") or "").strip() or default_backlog_owner()
    limit = _int(args.get("limit"), default=20, minimum=1, maximum=50)
    return {
        "owner": owner.upper(),
        "items": list_backlog_items(context.settings, owner=owner, limit=limit),
    }


def _brief_run(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    limit = _int(args.get("limit"), default=100, minimum=1, maximum=200)
    record_backlog = bool(args.get("record_backlog", True))
    result = run_brief(
        context.settings,
        limit=limit,
        record_backlog=record_backlog,
        db=context.db,
    )
    return {"ok": True, "result": result.to_dict()}


def _goal_start(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    brief = str(args.get("brief") or "").strip()
    if not brief:
        raise ToolExecutionError("brief is required")

    project = str(args.get("project") or "").strip()
    raw_cwd = str(args.get("cwd") or "").strip()
    cwd = Path(raw_cwd).expanduser() if raw_cwd else None
    if not project and cwd is None:
        cwd = context.cwd
        project = context.project_id or context.cwd.name

    session = context.db.get_bemine_session(context.session_id) or {}
    channel_id = str(args.get("channel_id") or "").strip() or str(session.get("channel_id") or "")
    interval = (
        _int(args.get("interval_secs"), default=600, minimum=30, maximum=86400)
        if args.get("interval_secs") is not None
        else 0
    )

    try:
        result = start_goal(
            context.settings,
            project=project,
            brief=brief,
            cwd=cwd,
            runtime=str(args.get("runtime") or ""),
            worker_name=str(args.get("worker") or ""),
            interval_secs=interval,
            channel_id=channel_id,
            command=str(args.get("command") or ""),
            target_device=str(args.get("target_device") or ""),
            db=context.db,
        )
    except (ValueError, WorkerError) as exc:
        raise ToolExecutionError(str(exc)) from exc

    return {
        "ok": True,
        "worker": result.worker.to_dict(),
        "watcher": dict(result.watcher),
        "prompt_len": len(result.prompt),
    }


def _goal_status(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    query = str(args.get("query") or "").strip()
    sessions = list_goal_sessions(context.settings, status=query, db=context.db)
    return {"ok": True, "sessions": [session.to_dict() for session in sessions]}


def _discord_threads(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    query = str(args.get("query") or "").strip()
    limit = _int(args.get("limit"), default=12, minimum=1, maximum=50)
    rows = DiscordStore(context.settings.db_path).channel_summaries(
        limit=limit,
        query=query,
    )
    return {"ok": True, "threads": redact_message_rows(rows)}


def _discord_transcript(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    query = str(args.get("query") or "").strip()
    channel_id = str(args.get("channel_id") or "").strip()
    if not channel_id and not query:
        session = context.db.get_bemine_session(context.session_id) or {}
        channel_id = str(session.get("channel_id") or "")
    limit = _int(args.get("limit"), default=30, minimum=1, maximum=100)
    rows = DiscordStore(context.settings.db_path).transcript_messages(
        channel_id=channel_id,
        query=query,
        limit=limit,
    )
    return {"ok": True, "messages": redact_message_rows(rows)}


def _channel_routes(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    from ..discord.dispatch import (
        DISPATCH_CONFIG_NAME,
        load_project_channels,
        project_channels_from_db,
    )

    query = str(args.get("query") or "").strip()
    limit = _int(args.get("limit"), default=50, minimum=1, maximum=200)
    config_path = context.settings.state_dir / DISPATCH_CONFIG_NAME
    watchers = {
        str(row.get("project_id") or ""): dict(row)
        for row in context.db.list_active_project_watchers()
    }

    configured = [
        _project_channel_route_dict(channel, context=context, watchers=watchers)
        for channel in load_project_channels(context.settings.state_dir).values()
    ]
    indexed = [
        _project_channel_route_dict(channel, context=context, watchers=watchers)
        for channel in project_channels_from_db(context.db, limit=limit)
    ]
    if query:
        configured = [row for row in configured if _route_matches(row, query)]
        indexed = [row for row in indexed if _route_matches(row, query)]

    return {
        "ok": True,
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "configured": configured[:limit],
        "indexed": indexed[:limit],
    }


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


def _deploy_control(args: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
    from bemine.deploy.pipeline import request_deploy
    from bemine.deploy.state import DeployState, state_path

    action = str(args.get("action") or "status")
    session = context.db.get_bemine_session(context.session_id) or {}
    channel_id = str(session.get("channel_id") or "")

    if action == "request":
        ok, msg = request_deploy(context.settings, channel_id=channel_id,
                                 requested_by=context.session_id)
        return {"ok": ok, "message": msg}

    st = DeployState.load(state_path(context.settings))
    return {"ok": True, "phase": st.phase, "target_sha": st.target_sha[:8],
            "rollback_sha": st.rollback_sha[:8]}


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


def _project_channel_route_dict(
    channel: Any,
    *,
    context: ToolContext,
    watchers: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    project_id = channel.project_id or channel.channel_name
    watcher = watchers.get(project_id) or {}
    return {
        "channel_name": channel.channel_name,
        "cwd": str(channel.cwd),
        "project_id": channel.project_id,
        "project_name": channel.project_name,
        "runtime": channel.runtime,
        "latest_channel_id": context.db.get_channel_id_for_project(project_id) or "",
        "watcher_channel_id": str(watcher.get("channel_id") or ""),
        "watcher_status": str(watcher.get("status") or ""),
    }


def _route_matches(row: Mapping[str, Any], query: str) -> bool:
    needle = query.casefold()
    return any(needle in str(value or "").casefold() for value in row.values())


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
