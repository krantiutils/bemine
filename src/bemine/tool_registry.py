from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import BemineSettings, config_bool, config_string
from .db import BemineDB
from .status import _redact, collect_fleet_status


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


class ToolExecutionError(RuntimeError):
    pass


class BemineToolRegistry:
    def __init__(self, tools: list[BemineTool] | None = None) -> None:
        self._tools = {tool.name: tool for tool in (tools or default_tools())}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_anthropic() for tool in self._tools.values()]

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
            description="Run a shell command in the project root or an allowed project subdirectory.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["command"],
            },
            handler=_shell_run,
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
    cwd = _resolve_project_path(str(args.get("cwd") or "."), context.cwd)
    timeout = _int(args.get("timeout"), default=60, minimum=1, maximum=120)
    proc = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "cwd": str(cwd),
        "returncode": proc.returncode,
        "stdout": _redact(proc.stdout[-12000:]),
        "stderr": _redact(proc.stderr[-12000:]),
    }


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
