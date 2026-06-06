from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import SessionRecord
from ..util import compact_text, stable_id


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), str):
            return value["content"]
    return ""


def discover_codex_sessions(codex_home: Path, *, device_id: str) -> list[SessionRecord]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    records: list[SessionRecord] = []
    for path in sorted(sessions_dir.rglob("*.jsonl")):
        record = parse_codex_session(path, device_id=device_id)
        if record:
            records.append(record)
    return records


def parse_codex_session(path: Path, *, device_id: str) -> SessionRecord | None:
    session_id = ""
    cwd = ""
    started_at = ""
    updated_at = ""
    model = ""
    message_count = 0
    tool_count = 0
    snippets: list[str] = []

    for obj in _read_jsonl(path):
        timestamp = str(obj.get("timestamp") or "")
        if timestamp:
            updated_at = timestamp
            if not started_at:
                started_at = timestamp
        typ = obj.get("type")
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if typ == "session_meta":
            session_id = str(payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            started_at = str(payload.get("timestamp") or started_at)
            updated_at = str(payload.get("timestamp") or updated_at)
            model = str(payload.get("model") or payload.get("model_provider") or model)
        elif typ == "turn_context":
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or model)
        elif typ == "response_item":
            role = str(payload.get("role") or "")
            item_type = str(payload.get("type") or "")
            if item_type == "message" and role in {"user", "assistant"}:
                message_count += 1
                text = _text_from_content(payload.get("content"))
                if text:
                    snippets.append(f"{role}: {compact_text(text)}")
            elif item_type in {"function_call", "local_shell_call"}:
                tool_count += 1
        elif typ == "event_msg":
            event_type = str(payload.get("type") or "")
            if event_type and "tool" in event_type:
                tool_count += 1

    if not session_id:
        session_id = stable_id(str(path), prefix="codex_")
    if not cwd:
        return None
    title = snippets[-1] if snippets else path.stem
    summary = " | ".join(snippets[-3:]) if snippets else f"Codex session in {cwd}"
    return SessionRecord(
        session_id=f"codex:{session_id}",
        runtime="codex",
        device_id=device_id,
        cwd=cwd,
        source_path=path,
        started_at=started_at,
        updated_at=updated_at or started_at,
        model=model,
        title=compact_text(title, limit=180),
        summary=compact_text(summary, limit=700),
        message_count=message_count,
        tool_count=tool_count,
    )


def discover_claude_sessions(claude_home: Path, *, device_id: str) -> list[SessionRecord]:
    projects_dir = claude_home / "projects"
    if not projects_dir.exists():
        return []
    records: list[SessionRecord] = []
    for path in sorted(projects_dir.rglob("*.jsonl")):
        record = parse_claude_session(path, device_id=device_id)
        if record:
            records.append(record)
    return records


def parse_claude_session(path: Path, *, device_id: str) -> SessionRecord | None:
    session_id = ""
    cwd = ""
    started_at = ""
    updated_at = ""
    model = ""
    branch = ""
    message_count = 0
    tool_count = 0
    snippets: list[str] = []

    for obj in _read_jsonl(path):
        timestamp = str(obj.get("timestamp") or "")
        if timestamp:
            updated_at = timestamp
            if not started_at:
                started_at = timestamp
        session_id = str(obj.get("sessionId") or session_id)
        cwd = str(obj.get("cwd") or cwd)
        branch = str(obj.get("gitBranch") or branch)
        typ = str(obj.get("type") or "")
        if typ in {"user", "assistant"}:
            message_count += 1
            msg = obj.get("message") or {}
            if isinstance(msg, dict):
                model = str(msg.get("model") or model)
                text = _text_from_content(msg.get("content"))
                if text:
                    snippets.append(f"{typ}: {compact_text(text)}")
        elif typ == "attachment":
            tool_count += 1
        elif typ == "system" and obj.get("subtype"):
            snippets.append(f"system: {compact_text(str(obj.get('subtype')))}")

    if not session_id:
        session_id = stable_id(str(path), prefix="claude_")
    if not cwd:
        return None
    title = snippets[-1] if snippets else path.stem
    branch_text = f" branch={branch}" if branch else ""
    summary = " | ".join(snippets[-3:]) if snippets else f"Claude session in {cwd}{branch_text}"
    return SessionRecord(
        session_id=f"claude:{session_id}",
        runtime="claude",
        device_id=device_id,
        cwd=cwd,
        source_path=path,
        started_at=started_at,
        updated_at=updated_at or started_at,
        model=model,
        title=compact_text(title, limit=180),
        summary=compact_text(summary, limit=700),
        message_count=message_count,
        tool_count=tool_count,
    )


def discover_kimi_sessions(kimi_home: Path, *, device_id: str) -> list[SessionRecord]:
    if not kimi_home.exists():
        return []
    records: list[SessionRecord] = []
    for path in sorted(kimi_home.rglob("*.jsonl")):
        record = parse_kimi_session(path, device_id=device_id)
        if record:
            records.append(record)
    return records


def parse_kimi_session(path: Path, *, device_id: str) -> SessionRecord | None:
    snippets: list[str] = []
    started_at = ""
    updated_at = ""
    cwd = ""
    count = 0
    for obj in _read_jsonl(path):
        count += 1
        timestamp = str(obj.get("timestamp") or obj.get("created_at") or "")
        if timestamp:
            updated_at = timestamp
            if not started_at:
                started_at = timestamp
        cwd = str(obj.get("cwd") or obj.get("project") or cwd)
        text = _text_from_content(obj.get("message") or obj.get("content") or obj.get("text"))
        if text:
            snippets.append(compact_text(text))
    if not cwd or not count:
        return None
    session_id = stable_id(str(path), prefix="kimi_")
    summary = " | ".join(snippets[-3:]) if snippets else f"Kimi session in {cwd}"
    return SessionRecord(
        session_id=f"kimi:{session_id}",
        runtime="kimi",
        device_id=device_id,
        cwd=cwd,
        source_path=path,
        started_at=started_at,
        updated_at=updated_at or started_at,
        model="",
        title=compact_text(snippets[-1] if snippets else path.stem, limit=180),
        summary=compact_text(summary, limit=700),
        message_count=count,
        tool_count=0,
    )

