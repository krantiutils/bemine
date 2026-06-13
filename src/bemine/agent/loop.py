from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.providers.types import NormalizedResponse, ToolCall
from bemine.context.tool_registry import BemineToolRegistry, ToolContext
from bemine.agent.clients import LLMClient

ProgressCallback = Callable[[str], None]


def _progress_label(name: str, args: dict) -> str:
    """A human-readable, redacted one-liner for a tool call — shows WHAT it's doing
    (the command/path/query), not just the tool name."""
    from bemine.index.status import _redact

    key = {"shell_run": "command", "file_read": "path", "file_search": "query"}.get(name)
    detail = str(args.get(key, "")) if key else ""
    if not detail and name == "audit_list":
        detail = str(args.get("category") or args.get("event_type") or "")
    if not detail:
        return name
    detail = _redact(detail).replace("`", "'").replace("\n", " ").strip()
    if len(detail) > 160:
        detail = detail[:160] + "…"
    return f"{name}: {detail}"


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
            system: str, progress_callback: ProgressCallback | None = None,
            cancel_event: Any | None = None) -> LoopResult:
        model = self._session_model(session_id)
        # Each stored row already IS a complete OpenAI-shape message dict; load it
        # directly so a persist/resume round-trip is the identity. Do NOT nest the
        # stored dict under "content" (that double-wraps assistant/tool turns and
        # breaks tool linkage on resume).
        #
        # Be resilient to legacy/foreign rows: the same session table may hold
        # messages written by the old native_runtime path in Anthropic content-block
        # format (a JSON *list*, not a {"role": ...} dict). Skip anything that isn't a
        # well-formed message dict so resume never crashes on mixed history.
        messages: list[dict] = []
        for m in self.db.list_bemine_session_messages(session_id=session_id):
            try:
                stored = json.loads(m["content_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(stored, dict) and isinstance(stored.get("role"), str):
                messages.append(stored)
        user_msg = {"role": "user", "content": user_message}
        messages.append(user_msg)
        self.db.append_bemine_session_message(
            session_id=session_id, role="user", content_json=json.dumps(user_msg),
        )

        context = ToolContext(settings=self.settings, db=self.db,
                              session_id=session_id, project_id=project_id, cwd=cwd)
        tool_schemas = self.tools.openai_schemas()
        tool_call_count = 0
        response: NormalizedResponse | None = None

        session_row = self.db.get_bemine_session(session_id)
        runtime = (session_row or {}).get("runtime", "")

        for iteration in range(1, self.max_iterations + 1):
            if cancel_event is not None and cancel_event.is_set():
                return LoopResult(final_text="", tool_calls=tool_call_count,
                                  iterations=iteration - 1, stopped_reason="cancelled")
            response = self.client.complete(messages=messages, tools=tool_schemas,
                                            model=model, system=system)
            # Check immediately after the blocking HTTP call returns: a newer message
            # may have interrupted while we were waiting for the API.  Without this
            # check the ghost thread would write assistant/tool rows to the session DB
            # AFTER the new AgentLoop has already started writing its own rows,
            # producing an interleaved history that triggers API 400 errors on every
            # subsequent dispatch.
            if cancel_event is not None and cancel_event.is_set():
                return LoopResult(final_text="", tool_calls=tool_call_count,
                                  iterations=iteration, stopped_reason="cancelled")
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
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
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
        # Hit the tool-step limit while still calling tools. Make one final, tool-FREE
        # call so the user gets an actual answer instead of silence ("(no output)").
        if cancel_event is not None and cancel_event.is_set():
            return LoopResult(final_text="", tool_calls=tool_call_count,
                              iterations=self.max_iterations, stopped_reason="cancelled")
        nudge = {
            "role": "user",
            "content": (
                "You've reached the tool-step limit. Stop using tools and give your best "
                "final answer now, based on what you've already gathered."
            ),
        }
        try:
            final = self.client.complete(messages=messages + [nudge], tools=[],
                                         model=model, system=system)
            final_text = (final.content or "").strip()
        except Exception:
            final_text = ""
        if not final_text:
            final_text = ((response.content or "") if response else "").strip() or (
                f"(stopped after {self.max_iterations} tool steps without a final answer "
                "— ask me to continue)"
            )
        self.db.append_bemine_session_message(
            session_id=session_id, role="assistant",
            content_json=json.dumps({"role": "assistant", "content": final_text}),
        )
        return LoopResult(final_text=final_text, tool_calls=tool_call_count,
                          iterations=self.max_iterations, stopped_reason="max_iterations")

    def _run_tool(self, tc: ToolCall, context: ToolContext,
                  progress_callback: ProgressCallback | None) -> str:
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            if progress_callback:
                progress_callback(f"{tc.name}: (invalid args)")
            return json.dumps({"error": "invalid tool arguments JSON"})
        if progress_callback:
            progress_callback(_progress_label(tc.name, args))
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
        model = str(row["model"]) if row and row["model"] else ""
        if not model:
            raise ValueError(
                f"bemine session {session_id!r} has no stored model id; "
                "cannot select a provider model for the agent loop."
            )
        return model
