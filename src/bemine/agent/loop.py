from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.providers.types import NormalizedResponse, ToolCall
from bemine.tool_registry import BemineToolRegistry, ToolContext
from bemine.agent.clients import LLMClient

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class LoopResult:
    final_text: str
    tool_calls: int
    iterations: int
    stopped_reason: str  # "stop" | "max_iterations"


class AgentLoop:
    """Bemine's own provider-agnostic turn loop. Reuses vendored transports via the
    injected LLMClient and Bemine's tool registry; persists to bemine_sessions."""

    def __init__(self, *, settings: BemineSettings, db: BemineDB, client: LLMClient,
                 tools: BemineToolRegistry | None = None, max_iterations: int = 8) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.tools = tools or BemineToolRegistry()
        self.max_iterations = max_iterations

    def run(self, *, session_id: str, project_id: str, cwd: Path, user_message: str,
            system: str, progress_callback: ProgressCallback | None = None) -> LoopResult:
        model = self._session_model(session_id)
        messages: list[dict] = [
            {"role": m["role"], "content": json.loads(m["content_json"])}
            for m in self.db.list_bemine_session_messages(session_id=session_id)
        ]
        messages.append({"role": "user", "content": user_message})
        self.db.append_bemine_session_message(
            session_id=session_id, role="user", content_json=json.dumps(user_message),
        )

        context = ToolContext(settings=self.settings, db=self.db,
                              session_id=session_id, project_id=project_id, cwd=cwd)
        tool_schemas = self.tools.schemas()
        tool_call_count = 0
        response: NormalizedResponse | None = None

        session_row = self.db.get_bemine_session(session_id)
        runtime = (session_row or {}).get("runtime", "")

        for iteration in range(1, self.max_iterations + 1):
            response = self.client.complete(messages=messages, tools=tool_schemas,
                                            model=model, system=system)
            usage = getattr(response, "usage", None)
            if usage is not None:
                in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
                total_tok = int(getattr(usage, "total_tokens", 0) or 0)
                if total_tok == 0 and (in_tok or out_tok):
                    total_tok = in_tok + out_tok
                self.db.record_usage_sample(
                    provider=runtime or model,
                    raw_json="{}",
                    runtime=runtime or None,
                    session_id=session_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=total_tok,
                )
            assistant_msg: dict = {"role": "assistant", "content": response.content or ""}
            if response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                ]
            messages.append(assistant_msg)
            self.db.append_bemine_session_message(
                session_id=session_id, role="assistant", content_json=json.dumps(assistant_msg),
            )
            if not response.tool_calls:
                return LoopResult(final_text=response.content or "", tool_calls=tool_call_count,
                                  iterations=iteration, stopped_reason="stop")
            for tc in response.tool_calls:
                tool_call_count += 1
                result_text = self._run_tool(tc, context, progress_callback)
                tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                messages.append(tool_msg)
                self.db.append_bemine_session_message(
                    session_id=session_id, role="tool", content_json=json.dumps(tool_msg),
                )
        return LoopResult(final_text=(response.content if response else "") or "",
                          tool_calls=tool_call_count, iterations=self.max_iterations,
                          stopped_reason="max_iterations")

    def _run_tool(self, tc: ToolCall, context: ToolContext,
                  progress_callback: ProgressCallback | None) -> str:
        if progress_callback:
            progress_callback(f"tool: {tc.name}")
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid tool arguments JSON"})
        self.db.record_tool_call_started(
            session_id=context.session_id,
            tool_call_id=tc.id,
            tool_name=tc.name,
            arguments_json=tc.arguments or "{}",
        )
        try:
            payload = self.tools.execute(tc.name, args, context)
            text = json.dumps(payload)
            status = "ok"
        except Exception as exc:
            text = json.dumps({"error": str(exc)})
            status = "error"
        self.db.record_tool_call_finished(
            session_id=context.session_id,
            tool_call_id=tc.id,
            status=status,
            result_json=text,
        )
        return text

    def _session_model(self, session_id: str) -> str:
        row = self.db.get_bemine_session(session_id)
        return str(row["model"]) if row and row["model"] else self.settings.device_id
