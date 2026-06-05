from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BemineSettings, config_int
from .db import BemineDB
from .kimi_handler import JsonTransport, KimiHandler
from .status import _redact
from .tool_registry import BemineToolRegistry, ToolContext, ToolExecutionError

ProgressCallback = Callable[[str, str, str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class NativeRunResult:
    ok: bool
    text: str
    elapsed_seconds: float
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class KimiNativeRuntime:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        transport: JsonTransport | None = None,
        tools: BemineToolRegistry | None = None,
        max_turns: int | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.settings = settings
        self.db = BemineDB(settings.db_path)
        self.handler = KimiHandler(settings.hermes_home, transport=transport)
        self.tools = tools or BemineToolRegistry()
        self.max_turns = max_turns or config_int("kimi", "tool_max_turns", 6)
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        session_id: str,
        channel_id: str,
        project_id: str,
        cwd: Path,
        prompt: str,
        system: str,
        progress_callback: ProgressCallback | None = None,
    ) -> NativeRunResult:
        return asyncio.run(
            self.run_async(
                session_id=session_id,
                channel_id=channel_id,
                project_id=project_id,
                cwd=cwd,
                prompt=prompt,
                system=system,
                progress_callback=progress_callback,
            )
        )

    async def run_async(
        self,
        *,
        session_id: str,
        channel_id: str,
        project_id: str,
        cwd: Path,
        prompt: str,
        system: str,
        progress_callback: ProgressCallback | None = None,
    ) -> NativeRunResult:
        started = time.monotonic()
        config = self.handler.status()
        cwd = cwd.expanduser().resolve()
        self.db.upsert_bemine_session(
            session_id=session_id,
            channel_id=channel_id,
            project_id=project_id,
            runtime="kimi",
            model=config.model,
            device_id=self.settings.device_id,
            cwd=str(cwd),
            status="active",
        )

        messages = self._load_messages(session_id)
        user_message = {"role": "user", "content": prompt}
        messages.append(user_message)
        self._append_message(session_id, user_message)

        max_tokens = max(1, config_int("kimi", "dispatch_max_tokens", 1600))
        input_tokens = 0
        output_tokens = 0
        final_text = ""

        for turn_index in range(max(1, min(self.max_turns, 20))):
            response = await self.handler.messages_async(
                messages,
                tools=self.tools.schemas(),
                system=system,
                max_tokens=max_tokens,
                timeout=self.timeout_seconds,
            )
            self._record_usage(session_id, response)
            usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
            input_tokens += _int(usage.get("input_tokens"))
            output_tokens += _int(usage.get("output_tokens"))

            assistant_message = {
                "role": "assistant",
                "content": _content_blocks(response),
            }
            messages.append(assistant_message)
            self._append_message(session_id, assistant_message)
            final_text = _redact(_text_from_blocks(assistant_message["content"]))

            tool_uses = _tool_uses(assistant_message["content"])
            if not tool_uses:
                elapsed = time.monotonic() - started
                return NativeRunResult(
                    ok=True,
                    text=final_text or "Kimi finished without a final message.",
                    elapsed_seconds=elapsed,
                    model=str(response.get("model") or config.model),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                )

            tool_results = []
            for tool_use in tool_uses:
                tool_result = self._execute_tool(
                    session_id=session_id,
                    project_id=project_id,
                    cwd=cwd,
                    tool_use=tool_use,
                    progress_callback=progress_callback,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.tool_id,
                        "content": json.dumps(tool_result, sort_keys=True),
                        "is_error": not bool(tool_result.get("ok")),
                    }
                )

            tool_message = {"role": "user", "content": tool_results}
            messages.append(tool_message)
            self._append_message(session_id, tool_message)

            if turn_index == self.max_turns - 1:
                break

        elapsed = time.monotonic() - started
        return NativeRunResult(
            ok=False,
            text=(
                final_text
                or f"Kimi reached the configured tool turn limit ({self.max_turns})."
            ),
            elapsed_seconds=elapsed,
            model=config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    def _load_messages(self, session_id: str) -> list[dict[str, Any]]:
        limit = max(1, config_int("kimi", "session_message_window", 24))
        messages: list[dict[str, Any]] = []
        for row in self.db.list_bemine_session_messages(
            session_id=session_id,
            limit=limit,
        ):
            try:
                content = json.loads(str(row["content_json"]))
            except json.JSONDecodeError:
                content = str(row["content_json"] or "")
            messages.append({"role": str(row["role"]), "content": content})
        return messages

    def _append_message(self, session_id: str, message: Mapping[str, Any]) -> None:
        self.db.append_bemine_session_message(
            session_id=session_id,
            role=str(message.get("role") or ""),
            content_json=json.dumps(message.get("content"), sort_keys=True),
        )

    def _record_usage(self, session_id: str, response: Mapping[str, Any]) -> None:
        usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        self.db.record_usage_sample(
            provider=self.handler.status().provider,
            account="kimi",
            runtime="kimi",
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            raw_json=json.dumps(response, sort_keys=True),
        )

    def _execute_tool(
        self,
        *,
        session_id: str,
        project_id: str,
        cwd: Path,
        tool_use: "_ToolUse",
        progress_callback: ProgressCallback | None,
    ) -> Mapping[str, Any]:
        arguments_json = json.dumps(tool_use.arguments, sort_keys=True)
        self.db.record_tool_call_started(
            session_id=session_id,
            tool_call_id=tool_use.tool_id,
            tool_name=tool_use.name,
            arguments_json=arguments_json,
        )
        if progress_callback:
            progress_callback(
                "kimi",
                "tool.started",
                f"🛠️ tool: `{tool_use.name}`",
                {"tool_name": tool_use.name, "arguments": tool_use.arguments},
            )
        started = time.monotonic()
        status = "success"
        try:
            payload = self.tools.execute(
                tool_use.name,
                tool_use.arguments,
                ToolContext(
                    settings=self.settings,
                    db=self.db,
                    session_id=session_id,
                    project_id=project_id,
                    cwd=cwd,
                ),
            )
            result: Mapping[str, Any] = {"ok": True, "result": payload}
        except (ToolExecutionError, OSError, RuntimeError, ValueError) as exc:
            status = "error"
            result = {"ok": False, "error": _redact(str(exc))}

        result_json = json.dumps(result, sort_keys=True)
        self.db.record_tool_call_finished(
            session_id=session_id,
            tool_call_id=tool_use.tool_id,
            status=status,
            result_json=result_json,
        )
        elapsed = time.monotonic() - started
        self.db.record_audit_event(
            category="tool",
            event_type="kimi_tool_call_finished",
            actor="bemine-native",
            device_id=self.settings.device_id,
            project_id=project_id,
            session_id=session_id,
            payload_json=json.dumps(
                {
                    "tool_call_id": tool_use.tool_id,
                    "tool_name": tool_use.name,
                    "status": status,
                    "elapsed_seconds": round(elapsed, 3),
                },
                sort_keys=True,
            ),
        )
        if progress_callback:
            icon = "✅" if status == "success" else "⚠️"
            label = "done" if status == "success" else "failed"
            progress_callback(
                "kimi",
                "tool.completed",
                f"{icon} tool {label}: `{tool_use.name}`",
                {"tool_name": tool_use.name, "status": status, "result": result},
            )
        return result


@dataclass(frozen=True)
class _ToolUse:
    tool_id: str
    name: str
    arguments: Mapping[str, Any]


def _content_blocks(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = response.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, Mapping)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _text_from_blocks(content: object) -> str:
    parts: list[str] = []
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _tool_uses(content: object) -> list[_ToolUse]:
    if not isinstance(content, list):
        return []
    tool_uses: list[_ToolUse] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        arguments = block.get("input")
        tool_uses.append(
            _ToolUse(
                tool_id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                arguments=arguments if isinstance(arguments, Mapping) else {},
            )
        )
    return [tool_use for tool_use in tool_uses if tool_use.tool_id and tool_use.name]


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
