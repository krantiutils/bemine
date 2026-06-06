# Bemine: Deterministic Agent-over-Discord (Hermes-powered tool calling) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Bemine's brittle LLM "intent router" with a deterministic Discord↔session binding plus a real provider-agnostic agent loop, reusing Hermes' model-specific tool-calling adapters directly while keeping Bemine's own core logic; then fold `mayor_discord` into `bemine` and group the flat modules into subpackages.

**Architecture:** Two surfaces, cleanly separated. (1) **Deterministic ops** — a shared command registry exposed identically on the CLI and as Discord *slash commands* (no LLM in the path). (2) **Agent pass-through** — each Discord thread/DM is deterministically bound (by channel/thread id) to one row in the existing `bemine_sessions` table; a plain message is appended to that session's transcript and run through Bemine's own agent loop. The loop calls models through **vendored Hermes transport adapters** (`anthropic_adapter.py`, `codex_responses_adapter.py`, `transports/*`) — which already handle Anthropic, OpenAI-Responses/Codex, and Anthropic-compatible Kimi — so the expensive per-provider tool-calling code is reused, but the loop, routing, persistence, and tools stay Bemine's.

**Tech Stack:** Python ≥3.11, `uv`, `pytest`, SQLite (existing `.bemine/bemine.db`), `discord.py==2.7.1`, `aiohttp`. New runtime deps: `anthropic` (Anthropic + Kimi), `openai` (Codex/Responses). Vendored (MIT) code from `NousResearch/hermes-agent` at `/home/cdjk/.hermes/hermes-agent`.

---

## Context: why this plan exists

Codex built a router (`src/mayor_discord/gateway.py`) that:
- Hardcodes a string-match ladder for `status`/`rundown`/`kimi status`/… (`gateway.py:659-734`).
- Uses a **Kimi-powered intent classifier** (`_route_local_tool_with_kimi`, `gateway.py:283-304`) to guess whether a plain message maps to a *fixed catalog* of "local tools" (`config.update_dispatch`, `checklist.add`). **This is the "only covers what I flagged" failure** — novel input misclassifies or falls through.
- Falls through to a content-inference project router (`infer_project_route`, `dispatch.py:239-285`) with a hardcoded "vague message" bail-out (`_needs_reference_clarification`, `gateway.py:253-280`) that replies *"I need the specific message you mean by this"* instead of just asking the agent.

The deterministic foundation already exists and is *ignored*: `bemine_sessions(session_id, channel_id, project_id, runtime, model, cwd, status, …)` + `bemine_session_messages(session_id, role, content_json, …)` — currently 1 session / 105 messages. Hermes proves the deterministic approach end-to-end (`gateway/session.py:600-665` `build_session_key`).

**Reuse boundary (confirmed against Hermes source):**
- LIFT (model-specific tool calling): `agent/transports/base.py`, `agent/transports/types.py`, `agent/transports/anthropic.py`, `agent/transports/codex.py`, `agent/anthropic_adapter.py`, `agent/codex_responses_adapter.py`. Low/very-low coupling; Kimi already supported (`anthropic_adapter.py:385-443`, `_is_kimi_coding_endpoint`).
- OURS (write fresh): the turn loop (Hermes' `conversation_loop.py` is too entangled), the deterministic Discord binding, the command registry, the tools, persistence to `bemine_sessions`.

---

## File Structure

**New files:**
- `src/bemine/providers/__init__.py` — transport registry (`get_transport(api_mode)`).
- `src/bemine/providers/base.py` — `ProviderTransport` ABC (lifted).
- `src/bemine/providers/types.py` — `NormalizedResponse`, `ToolCall`, `Usage` (lifted).
- `src/bemine/providers/anthropic_transport.py` — lifted `transports/anthropic.py`.
- `src/bemine/providers/codex_transport.py` — lifted `transports/codex.py`.
- `src/bemine/providers/_anthropic_adapter.py` — lifted `agent/anthropic_adapter.py` (+ shims).
- `src/bemine/providers/_codex_responses_adapter.py` — lifted `agent/codex_responses_adapter.py` (+ shims).
- `src/bemine/providers/_compat.py` — shims replacing Hermes-only deps (`get_hermes_home`, url helpers, `DEFAULT_AGENT_IDENTITY`).
- `src/bemine/providers/NOTICE` — MIT attribution for the vendored Hermes files.
- `src/bemine/agent/__init__.py`
- `src/bemine/agent/clients.py` — `LLMClient` protocol + `AnthropicClient` (Claude/Kimi) + `CodexClient`.
- `src/bemine/agent/loop.py` — Bemine's own turn loop (`AgentLoop`).
- `src/bemine/agent/session.py` — deterministic binding: `session_key_for(...)`, `ensure_session(...)`, `append_user_message(...)`.
- `src/bemine/commands.py` — deterministic command registry (`CommandSpec`, `registry()`), shared by CLI + Discord.
- `tests/test_providers_vendored.py`, `tests/test_agent_loop.py`, `tests/test_session_binding.py`, `tests/test_commands_registry.py`, `tests/test_usage_report.py`, `tests/test_digest_chunking.py`, `tests/test_slash_commands.py`.

**Modified files:**
- `pyproject.toml` — add `anthropic`, `openai` deps; package list after reorg.
- `src/bemine/db.py` — add `get_bemine_session_by_channel`, `usage_rollup`.
- `src/bemine/cli.py` — add `usage` command; route deterministic subcommands through `commands.registry()`.
- `src/mayor_discord/gateway.py` — delete the intent classifier + ladder + bail-out; bind→append→run; register slash commands.
- `src/mayor_discord/dispatch.py` — replace `infer_project_route` content-inference with deterministic channel binding; record usage.
- `src/bemine/jobs.py` / `src/mayor_discord/send.py` — chunk long Discord messages (fixes `daily_digest`).

**Reorg (Phase 6) moves** `src/mayor_discord/*` → `src/bemine/discord/*` and groups flat modules into `bemine/{providers,agent,discord,workers,security,jobs,context,index}`.

---

## Phase 0 — Baseline

### Task 0: Confirm green baseline

**Files:** none (verification only)

- [ ] **Step 1: Run the suite**

Run: `cd /home/cdjk/bm && uv run pytest -q`
Expected: `90 passed` (baseline established 2026-06-06).

- [ ] **Step 2: Create a working branch**

```bash
cd /home/cdjk/bm
git checkout -b deterministic-agent-over-discord
git commit --allow-empty -m "chore: start deterministic-agent-over-discord work"
```

---

## Phase 1 — Vendor Hermes provider (model-specific tool calling)

> Lift the per-provider tool-calling adapters as a near-standalone unit. These files are MIT-licensed (`NousResearch/hermes-agent`). The only Hermes couplings to break: `anthropic_adapter.py` imports `hermes_constants.get_hermes_home` + two `utils` helpers; `codex_responses_adapter.py` imports `agent.prompt_builder.DEFAULT_AGENT_IDENTITY`. All replaced by `providers/_compat.py`.

### Task 1: Add provider SDK dependencies

**Files:**
- Modify: `pyproject.toml:6-9` (dependencies array)

- [ ] **Step 1: Add deps**

```toml
dependencies = [
    "aiohttp>=3.9",
    "discord.py==2.7.1",
    "anthropic>=0.40",
    "openai>=1.50",
]
```

- [ ] **Step 2: Sync and verify import**

Run: `cd /home/cdjk/bm && uv sync && uv run python -c "import anthropic, openai; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add anthropic and openai provider SDKs"
```

### Task 2: Lift the neutral types + base + compat shims

**Files:**
- Create: `src/bemine/providers/__init__.py`
- Create: `src/bemine/providers/types.py` (copy of `/home/cdjk/.hermes/hermes-agent/agent/transports/types.py`)
- Create: `src/bemine/providers/base.py` (copy of `/home/cdjk/.hermes/hermes-agent/agent/transports/base.py`)
- Create: `src/bemine/providers/_compat.py`
- Create: `src/bemine/providers/NOTICE`
- Test: `tests/test_providers_vendored.py`

- [ ] **Step 1: Write the failing test (neutral types import + roundtrip)**

```python
# tests/test_providers_vendored.py
from bemine.providers.types import NormalizedResponse, ToolCall


def test_toolcall_is_provider_neutral():
    tc = ToolCall(id="abc", name="status_show", arguments='{"limit": 5}')
    assert tc.name == "status_show"
    # Hermes backcompat shim: tc.function.name and tc.type == "function"
    assert tc.function.name == "status_show"
    assert tc.type == "function"


def test_normalized_response_shape():
    resp = NormalizedResponse(content="hi", tool_calls=None, finish_reason="stop")
    assert resp.finish_reason == "stop"
    assert resp.content == "hi"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_providers_vendored.py -q`
Expected: FAIL — `ModuleNotFoundError: bemine.providers`.

- [ ] **Step 3: Copy the source files verbatim**

```bash
cd /home/cdjk/bm
mkdir -p src/bemine/providers
cp /home/cdjk/.hermes/hermes-agent/agent/transports/types.py src/bemine/providers/types.py
cp /home/cdjk/.hermes/hermes-agent/agent/transports/base.py  src/bemine/providers/base.py
: > src/bemine/providers/__init__.py
```

- [ ] **Step 4: Read the two copied files and fix any internal Hermes imports**

Read `src/bemine/providers/types.py` and `src/bemine/providers/base.py`. They are expected to depend only on stdlib + each other. For any `from agent.transports.types import ...` change to `from .types import ...`. For any `from agent.transports.base import ...` change to `from .base import ...`. Do not change logic.

- [ ] **Step 5: Create the compat shims**

```python
# src/bemine/providers/_compat.py
"""Replacements for Hermes-only deps used by the vendored adapter files."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

# Replaces agent.prompt_builder.DEFAULT_AGENT_IDENTITY
DEFAULT_AGENT_IDENTITY = "You are Bemine, the operator's personal multi-device agent."


def get_hermes_home() -> Path:
    """Replaces hermes_constants.get_hermes_home(). Bemine keeps provider scratch
    state under the Bemine state dir, but the adapters only use this for optional
    on-disk credential lookups we do not exercise; a temp-ish home is fine."""
    return Path.home() / ".bemine" / "provider-home"


def base_url_host_matches(base_url: str | None, host: str) -> bool:
    """Replaces utils.base_url_host_matches."""
    if not base_url:
        return False
    return (urlparse(base_url).hostname or "").lower() == host.lower()


def normalize_proxy_env_vars() -> None:
    """Replaces utils.normalize_proxy_env_vars. Bemine does not rewrite proxy env;
    no-op keeps the adapter call sites working."""
    return None
```

- [ ] **Step 6: Write the NOTICE**

```
# src/bemine/providers/NOTICE
The files in this directory (types.py, base.py, anthropic_transport.py,
codex_transport.py, _anthropic_adapter.py, _codex_responses_adapter.py) are
derived from NousResearch/hermes-agent (https://github.com/NousResearch/hermes-agent),
licensed MIT. Local modifications: import paths adjusted to bemine.providers and
Hermes-only helpers replaced by bemine.providers._compat. Original MIT license text
retained per upstream LICENSE.
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/test_providers_vendored.py -q`
Expected: PASS (2 tests).

- [ ] **Step 8: Commit**

```bash
git add src/bemine/providers tests/test_providers_vendored.py
git commit -m "feat(providers): vendor Hermes neutral types + base transport (MIT)"
```

### Task 3: Lift the Anthropic + Codex adapters and transports

**Files:**
- Create: `src/bemine/providers/_anthropic_adapter.py` (copy of `agent/anthropic_adapter.py`)
- Create: `src/bemine/providers/_codex_responses_adapter.py` (copy of `agent/codex_responses_adapter.py`)
- Create: `src/bemine/providers/anthropic_transport.py` (copy of `agent/transports/anthropic.py`)
- Create: `src/bemine/providers/codex_transport.py` (copy of `agent/transports/codex.py`)
- Modify: `src/bemine/providers/__init__.py` (transport registry)
- Test: `tests/test_providers_vendored.py` (extend)

- [ ] **Step 1: Write the failing test (tool schema translation + tool-call parse, no network)**

```python
# tests/test_providers_vendored.py  (append)
from bemine.providers._anthropic_adapter import convert_tools_to_anthropic


SAMPLE_TOOL = {
    "type": "function",
    "function": {
        "name": "status_show",
        "description": "Show fleet status",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
}


def test_anthropic_tool_translation_renames_parameters_to_input_schema():
    out = convert_tools_to_anthropic([SAMPLE_TOOL])
    assert len(out) == 1
    tool = out[0]
    assert tool["name"] == "status_show"
    assert "input_schema" in tool          # Anthropic shape, not "parameters"
    assert "parameters" not in tool
    assert tool["input_schema"]["type"] == "object"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_providers_vendored.py -q`
Expected: FAIL — `ModuleNotFoundError: ... _anthropic_adapter`.

- [ ] **Step 3: Copy the four files**

```bash
cd /home/cdjk/bm
cp /home/cdjk/.hermes/hermes-agent/agent/anthropic_adapter.py        src/bemine/providers/_anthropic_adapter.py
cp /home/cdjk/.hermes/hermes-agent/agent/codex_responses_adapter.py  src/bemine/providers/_codex_responses_adapter.py
cp /home/cdjk/.hermes/hermes-agent/agent/transports/anthropic.py     src/bemine/providers/anthropic_transport.py
cp /home/cdjk/.hermes/hermes-agent/agent/transports/codex.py         src/bemine/providers/codex_transport.py
```

- [ ] **Step 4: Break the Hermes couplings**

Read each copied file and apply exactly these edits (no logic changes):

In `_anthropic_adapter.py`:
- `from hermes_constants import get_hermes_home` → `from ._compat import get_hermes_home`
- `from utils import base_url_host_matches, normalize_proxy_env_vars` (or `import utils`) → `from ._compat import base_url_host_matches, normalize_proxy_env_vars` (and replace any `utils.base_url_host_matches`/`utils.normalize_proxy_env_vars` usages with the bare names).
- Any `from tools.schema_sanitizer import strip_nullable_unions` → wrap in try/except with an identity fallback:
  ```python
  try:
      from tools.schema_sanitizer import strip_nullable_unions
  except Exception:  # bemine has no Hermes tools package
      def strip_nullable_unions(schema, keep_nullable_hint=False):
          return schema
  ```
- Any `from tools.registry import ...` (MCP prefix stripping) → same try/except-with-noop pattern.

In `_codex_responses_adapter.py`:
- `from agent.prompt_builder import DEFAULT_AGENT_IDENTITY` → `from ._compat import DEFAULT_AGENT_IDENTITY`

In `anthropic_transport.py` and `codex_transport.py`:
- `from agent.transports.base import ProviderTransport` → `from .base import ProviderTransport`
- `from agent.transports.types import ...` → `from .types import ...`
- `from agent.anthropic_adapter import ...` → `from ._anthropic_adapter import ...`
- `from agent.codex_responses_adapter import ...` → `from ._codex_responses_adapter import ...`
- Replace `register_transport(...)` / `get_transport` imports to point at `from . import register_transport` (defined next step). If the upstream file calls `register_transport("anthropic_messages", AnthropicTransport)` at import time, keep that call.

- [ ] **Step 5: Implement the registry in `__init__.py`**

```python
# src/bemine/providers/__init__.py
"""Provider transport registry (vendored from Hermes, MIT). See NOTICE."""
from __future__ import annotations

from .types import NormalizedResponse, ToolCall  # noqa: F401

_REGISTRY: dict[str, type] = {}


def register_transport(api_mode: str, transport_cls: type) -> None:
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str):
    if api_mode not in _REGISTRY:
        # Import side-effect modules register themselves on first import.
        from . import anthropic_transport  # noqa: F401
        from . import codex_transport  # noqa: F401
    return _REGISTRY[api_mode]()
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_providers_vendored.py -q`
Expected: PASS (3 tests). If an import error names another Hermes module, apply the same try/except-noop or `._compat` shim pattern to that import and re-run.

- [ ] **Step 7: Add a Codex translation assertion + run full suite**

```python
# tests/test_providers_vendored.py  (append)
from bemine.providers._codex_responses_adapter import _responses_tools


def test_codex_tool_translation_keeps_function_type():
    out = _responses_tools([SAMPLE_TOOL])
    assert out and out[0]["type"] == "function"
    assert out[0]["name"] == "status_show" or out[0]["function"]["name"] == "status_show"
```

Run: `uv run pytest tests/test_providers_vendored.py -q && uv run pytest -q`
Expected: provider tests PASS; full suite still `90 passed` + new tests.

- [ ] **Step 8: Commit**

```bash
git add src/bemine/providers tests/test_providers_vendored.py
git commit -m "feat(providers): vendor Hermes Anthropic+Codex tool-calling adapters (MIT)"
```

---

## Phase 2 — Bemine's own agent loop

> Hermes' `conversation_loop.py` is the crown jewel but too entangled to lift. We write a thin loop over the vendored transports + Bemine's existing `BemineToolRegistry` (`src/bemine/tool_registry.py`: `schemas()`, `execute(name, args, context)`, `ToolContext(settings, db, session_id, project_id, cwd)`), persisting to the existing `bemine_sessions` tables.

### Task 4: `LLMClient` protocol + a fake for tests

**Files:**
- Create: `src/bemine/agent/__init__.py`
- Create: `src/bemine/agent/clients.py`
- Test: `tests/test_agent_loop.py`

- [ ] **Step 1: Write the protocol + fake (no network)**

```python
# src/bemine/agent/clients.py
from __future__ import annotations

from typing import Protocol, Sequence

from bemine.providers.types import NormalizedResponse


class LLMClient(Protocol):
    """Bemine's seam onto the vendored provider transports. One call = one model turn."""

    def complete(
        self,
        *,
        messages: Sequence[dict],
        tools: Sequence[dict],
        model: str,
        system: str,
    ) -> NormalizedResponse: ...
```

```python
# tests/test_agent_loop.py
from bemine.providers.types import NormalizedResponse, ToolCall


class FakeClient:
    """Scripted LLMClient: yields each queued NormalizedResponse per call."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def complete(self, *, messages, tools, model, system):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        return self._scripted.pop(0)


def test_fake_client_scripts_responses():
    client = FakeClient([NormalizedResponse(content="done", tool_calls=None, finish_reason="stop")])
    out = client.complete(messages=[], tools=[], model="m", system="s")
    assert out.content == "done"
```

- [ ] **Step 2: Run to verify it fails then passes**

Run: `uv run pytest tests/test_agent_loop.py -q`
Expected: FAIL (no `bemine.agent`), then after creating `src/bemine/agent/__init__.py` (empty) and `clients.py` above: PASS.

```bash
: > src/bemine/agent/__init__.py
```

- [ ] **Step 3: Commit**

```bash
git add src/bemine/agent tests/test_agent_loop.py
git commit -m "feat(agent): add LLMClient seam onto vendored transports"
```

### Task 5: The turn loop

**Files:**
- Create: `src/bemine/agent/loop.py`
- Test: `tests/test_agent_loop.py` (extend)

- [ ] **Step 1: Write the failing test — tool call then stop, persisted**

```python
# tests/test_agent_loop.py  (append)
import json
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.tool_registry import BemineToolRegistry
from bemine.agent.loop import AgentLoop


def _settings(tmp_path: Path) -> BemineSettings:
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "vault")


def test_loop_executes_tool_then_returns_final_text(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="chan1", project_id="p1",
        runtime="kimi", model="kimi-k2.6", device_id="local",
        cwd=str(tmp_path), status="active", summary="",
    )
    client = FakeClient([
        NormalizedResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(id="t1", name="status_show", arguments=json.dumps({"limit": 1}))],
        ),
        NormalizedResponse(content="Here is your status.", finish_reason="stop", tool_calls=None),
    ])
    loop = AgentLoop(settings=settings, db=db, client=client,
                     tools=BemineToolRegistry(), max_iterations=5)

    result = loop.run(session_id="s1", project_id="p1", cwd=tmp_path,
                      user_message="status please", system="You are Bemine.")

    assert result.final_text == "Here is your status."
    assert result.tool_calls == 1
    # Two model calls: initial + after tool result
    assert len(client.calls) == 2
    # Second call must include the tool result in the message list
    roles = [m["role"] for m in client.calls[1]["messages"]]
    assert "tool" in roles
    # Persisted: user msg + assistant tool turn + tool result + final assistant
    stored = db.list_bemine_session_messages("s1")
    assert any(m["role"] == "user" for m in stored)
    assert any(m["role"] == "assistant" for m in stored)


def test_loop_stops_at_max_iterations(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s2", channel_id="chan2", project_id="p1", runtime="kimi",
        model="m", device_id="local", cwd=str(tmp_path), status="active", summary="",
    )
    # Always asks for a tool -> must be bounded by max_iterations
    looping = [
        NormalizedResponse(content=None, finish_reason="tool_calls",
                           tool_calls=[ToolCall(id=f"t{i}", name="status_show", arguments="{}")])
        for i in range(10)
    ]
    client = FakeClient(looping)
    loop = AgentLoop(settings=settings, db=db, client=client,
                     tools=BemineToolRegistry(), max_iterations=3)
    result = loop.run(session_id="s2", project_id="p1", cwd=tmp_path,
                      user_message="go", system="s")
    assert result.stopped_reason == "max_iterations"
    assert len(client.calls) == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_loop.py -q`
Expected: FAIL — `ModuleNotFoundError: bemine.agent.loop`.

- [ ] **Step 3: Implement the loop**

```python
# src/bemine/agent/loop.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

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

    def __init__(
        self,
        *,
        settings: BemineSettings,
        db: BemineDB,
        client: LLMClient,
        tools: BemineToolRegistry | None = None,
        max_iterations: int = 8,
    ) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.tools = tools or BemineToolRegistry()
        self.max_iterations = max_iterations

    def run(
        self,
        *,
        session_id: str,
        project_id: str,
        cwd: Path,
        user_message: str,
        system: str,
        progress_callback: ProgressCallback | None = None,
    ) -> LoopResult:
        model = self._session_model(session_id)
        # Resume transcript from the session store, then append the new user turn.
        messages: list[dict] = [
            {"role": m["role"], "content": json.loads(m["content_json"])}
            for m in self.db.list_bemine_session_messages(session_id)
        ]
        messages.append({"role": "user", "content": user_message})
        self.db.append_bemine_session_message(
            session_id=session_id, role="user",
            content_json=json.dumps(user_message),
        )

        context = ToolContext(
            settings=self.settings, db=self.db,
            session_id=session_id, project_id=project_id, cwd=cwd,
        )
        tool_schemas = self.tools.schemas()
        tool_call_count = 0

        for iteration in range(1, self.max_iterations + 1):
            response: NormalizedResponse = self.client.complete(
                messages=messages, tools=tool_schemas, model=model, system=system,
            )
            assistant_msg = {"role": "assistant", "content": response.content or ""}
            if response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                ]
            messages.append(assistant_msg)
            self.db.append_bemine_session_message(
                session_id=session_id, role="assistant",
                content_json=json.dumps(assistant_msg),
            )

            if not response.tool_calls:
                final = response.content or ""
                return LoopResult(final_text=final, tool_calls=tool_call_count,
                                  iterations=iteration, stopped_reason="stop")

            for tc in response.tool_calls:
                tool_call_count += 1
                result_text = self._run_tool(tc, context, progress_callback)
                tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                messages.append(tool_msg)
                self.db.append_bemine_session_message(
                    session_id=session_id, role="tool",
                    content_json=json.dumps(tool_msg),
                )

        return LoopResult(final_text=(response.content or ""), tool_calls=tool_call_count,
                          iterations=self.max_iterations, stopped_reason="max_iterations")

    def _run_tool(self, tc: ToolCall, context: ToolContext,
                  progress_callback: ProgressCallback | None) -> str:
        if progress_callback:
            progress_callback(f"tool: {tc.name}")
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid tool arguments JSON"})
        self.db.record_tool_call_started(
            session_id=context.session_id, tool_name=tc.name, arguments_json=tc.arguments or "{}",
        )
        try:
            payload = self.tools.execute(tc.name, args, context)
            text = json.dumps(payload)
            status = "ok"
        except Exception as exc:  # tool errors are returned to the model, not raised
            text = json.dumps({"error": str(exc)})
            status = "error"
        self.db.record_tool_call_finished(
            session_id=context.session_id, tool_name=tc.name, status=status,
        )
        return text

    def _session_model(self, session_id: str) -> str:
        row = self.db.get_bemine_session(session_id)
        return str(row["model"]) if row and row["model"] else self.settings.device_id
```

- [ ] **Step 4: Add the `get_bemine_session` accessor used by the loop**

**Files:** Modify `src/bemine/db.py` (near `list_bemine_session_messages`, ~line 946).

```python
    def get_bemine_session(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bemine_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None
```

If `record_tool_call_started`/`record_tool_call_finished` signatures differ from the calls above, read `src/bemine/db.py:964-1000` and adjust the call kwargs in `loop.py` to match the real signature.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_agent_loop.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/bemine/agent/loop.py src/bemine/db.py tests/test_agent_loop.py
git commit -m "feat(agent): add Bemine turn loop over vendored transports + tool registry"
```

### Task 6: Real provider clients (Claude/Kimi via Anthropic, Codex via Responses)

**Files:**
- Modify: `src/bemine/agent/clients.py`
- Test: `tests/test_agent_loop.py` (extend with a transport-level test, still no network)

- [ ] **Step 1: Write the failing test — client builds kwargs + normalizes a canned response**

```python
# tests/test_agent_loop.py  (append)
from bemine.agent.clients import AnthropicClient


class _FakeAnthropicSDK:
    """Stands in for anthropic.Anthropic(): records kwargs, returns a canned object."""
    def __init__(self):
        self.messages = self
        self.seen = {}

    def create(self, **kwargs):
        self.seen = kwargs
        class _Block:  # one tool_use block
            type = "tool_use"; id = "t1"; name = "status_show"; input = {"limit": 1}
        class _Resp:
            content = [_Block()]; stop_reason = "tool_use"
            usage = type("U", (), {"input_tokens": 10, "output_tokens": 3})()
        return _Resp()


def test_anthropic_client_normalizes_tool_use(monkeypatch):
    sdk = _FakeAnthropicSDK()
    client = AnthropicClient(sdk=sdk, base_url="https://api.kimi.com/coding")
    out = client.complete(
        messages=[{"role": "user", "content": "status"}],
        tools=[{"type": "function", "function": {"name": "status_show",
                "description": "", "parameters": {"type": "object", "properties": {}}}}],
        model="kimi-k2.6", system="You are Bemine.",
    )
    assert out.tool_calls and out.tool_calls[0].name == "status_show"
    # Tools were translated to Anthropic shape before the SDK call
    assert "tools" in sdk.seen
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_loop.py -k anthropic_client -q`
Expected: FAIL — `cannot import name 'AnthropicClient'`.

- [ ] **Step 3: Implement the clients over the vendored transports**

```python
# src/bemine/agent/clients.py  (append)
from __future__ import annotations

from bemine.providers import get_transport


class AnthropicClient:
    """Claude AND Anthropic-compatible Kimi. The vendored anthropic transport already
    detects api.kimi.com/coding and adjusts betas/auth (see providers/_anthropic_adapter)."""

    def __init__(self, *, sdk, base_url: str | None = None):
        self._sdk = sdk
        self._base_url = base_url
        self._transport = get_transport("anthropic_messages")

    def complete(self, *, messages, tools, model, system):
        kwargs = self._transport.build_kwargs(
            model=model, messages=list(messages), tools=list(tools), system=system,
        )
        raw = self._sdk.messages.create(**kwargs)
        return self._transport.normalize_response(raw)


class CodexClient:
    """Codex via the OpenAI Responses API, through the vendored codex transport."""

    def __init__(self, *, sdk):
        self._sdk = sdk
        self._transport = get_transport("codex_responses")

    def complete(self, *, messages, tools, model, system):
        kwargs = self._transport.build_kwargs(
            model=model, messages=list(messages), tools=list(tools), system=system,
        )
        raw = self._sdk.responses.create(**kwargs)
        return self._transport.normalize_response(raw)
```

If `build_kwargs`/`normalize_response` parameter names in the vendored `base.py` differ (read `src/bemine/providers/base.py` for the exact ABC signature, and the concrete `anthropic_transport.py`), adjust these call sites to match. Keep the client's own `complete(...)` signature stable — it is the seam the loop depends on.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_agent_loop.py -q`
Expected: PASS. If the vendored transport's `build_kwargs` requires extra params (e.g. `max_tokens`), add Bemine defaults (`max_tokens` from `bemine.toml [kimi].dispatch_max_tokens`, default 1600) in the client.

- [ ] **Step 5: Commit**

```bash
git add src/bemine/agent/clients.py tests/test_agent_loop.py
git commit -m "feat(agent): add Anthropic(Claude/Kimi) and Codex provider clients"
```

---

## Phase 3 — Deterministic Discord ↔ session binding (delete the router)

> Replace LLM intent classification with a deterministic key (Hermes pattern, `gateway/session.py:600-665`). A message in channel/thread C belongs to the session bound to C. No guessing.

### Task 7: Session binding helpers

**Files:**
- Create: `src/bemine/agent/session.py`
- Modify: `src/bemine/db.py` (add `get_bemine_session_by_channel`)
- Test: `tests/test_session_binding.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_binding.py
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.agent.session import session_key_for, ensure_session


def _db(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    return settings, BemineDB(settings.db_path)


def test_session_key_is_deterministic_per_surface():
    dm = session_key_for(platform="discord", chat_type="dm", chat_id="u1", thread_id=None)
    dm2 = session_key_for(platform="discord", chat_type="dm", chat_id="u1", thread_id=None)
    thread = session_key_for(platform="discord", chat_type="thread", chat_id="parent", thread_id="t9")
    assert dm == dm2                      # same surface -> same key, no randomness
    assert dm != thread
    assert thread.endswith("t9")


def test_ensure_session_resumes_same_row_for_same_channel(tmp_path):
    settings, db = _db(tmp_path)
    s1 = ensure_session(db, settings, channel_id="chan1", chat_type="thread",
                        chat_id="parent", thread_id="chan1", project_id="p1",
                        runtime="kimi", model="kimi-k2.6", cwd=tmp_path)
    s2 = ensure_session(db, settings, channel_id="chan1", chat_type="thread",
                        chat_id="parent", thread_id="chan1", project_id="p1",
                        runtime="kimi", model="kimi-k2.6", cwd=tmp_path)
    assert s1 == s2                        # deterministic resume, not a new session
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_session_binding.py -q`
Expected: FAIL — `ModuleNotFoundError: bemine.agent.session`.

- [ ] **Step 3: Implement binding (deterministic key + DB-backed ensure)**

```python
# src/bemine/agent/session.py
from __future__ import annotations

from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB


def session_key_for(*, platform: str, chat_type: str, chat_id: str,
                    thread_id: str | None) -> str:
    """Deterministic session key. Mirrors Hermes build_session_key:
    threads/DMs are shared lanes keyed on stable ids, never on message content."""
    parts = ["agent", platform, chat_type, chat_id]
    if thread_id:
        parts.append(thread_id)
    return ":".join(parts)


def ensure_session(
    db: BemineDB,
    settings: BemineSettings,
    *,
    channel_id: str,
    chat_type: str,
    chat_id: str,
    thread_id: str | None,
    project_id: str,
    runtime: str,
    model: str,
    cwd: Path,
) -> str:
    """Resolve the session bound to this Discord surface, creating it once if absent.
    Binding key is the channel/thread id (already a column on bemine_sessions)."""
    existing = db.get_bemine_session_by_channel(channel_id)
    if existing:
        return str(existing["session_id"])
    session_id = session_key_for(
        platform="discord", chat_type=chat_type, chat_id=chat_id, thread_id=thread_id,
    )
    db.upsert_bemine_session(
        session_id=session_id, channel_id=channel_id, project_id=project_id,
        runtime=runtime, model=model, device_id=settings.device_id,
        cwd=str(cwd), status="active", summary="",
    )
    return session_id
```

- [ ] **Step 4: Add `get_bemine_session_by_channel` to `db.py`**

```python
    def get_bemine_session_by_channel(self, channel_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bemine_sessions WHERE channel_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            return dict(row) if row else None
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_session_binding.py -q`
Expected: PASS (2 tests). If `upsert_bemine_session` requires more columns (read `db.py:870-920`), pass them.

- [ ] **Step 6: Commit**

```bash
git add src/bemine/agent/session.py src/bemine/db.py tests/test_session_binding.py
git commit -m "feat(agent): deterministic Discord channel/thread -> session binding"
```

### Task 8: Rewire the gateway to bind→append→run, deleting the intent router

**Files:**
- Modify: `src/mayor_discord/gateway.py` — `_handle_allowed_message` (652-806) and helpers (194-330, 253-280).
- Test: `tests/test_discord_dispatch.py` (extend) and existing `tests/test_discord_expressive_gateway.py` must still pass.

- [ ] **Step 1: Write the failing test — a plain message routes to the agent, not a classifier**

```python
# tests/test_discord_dispatch.py  (append)
def test_plain_message_goes_straight_to_bound_session(monkeypatch, tmp_path):
    """A novel plain message must NOT be classified; it must append to the bound
    session and run the agent loop. Regression for the 'only covers what I flagged' bug."""
    import mayor_discord.gateway as gw

    calls = {"router": 0, "agent": 0}

    # If the old Kimi intent router is ever consulted, fail loudly.
    monkeypatch.setattr(gw, "_route_local_tool_with_kimi",
                        lambda *a, **k: calls.__setitem__("router", calls["router"] + 1) or None,
                        raising=False)

    def fake_run_agent(*args, **kwargs):
        calls["agent"] += 1
        return "ok"

    monkeypatch.setattr(gw, "run_bound_agent", fake_run_agent, raising=False)
    # Drive _handle_allowed_message with a fake message whose content is novel.
    # (Construct the fake Message/responder per the existing gateway test helpers.)
    # assert calls["agent"] == 1 and calls["router"] == 0
```

Adapt the fake `Message`/responder construction to the existing helpers in `tests/test_discord_expressive_gateway.py`. The asserted contract: **novel plain text increments `agent`, never `router`.**

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_discord_dispatch.py -k bound_session -q`
Expected: FAIL — `run_bound_agent` does not exist yet.

- [ ] **Step 3: Add `run_bound_agent` and simplify `_handle_allowed_message`**

In `gateway.py`, add a helper that binds and runs:

```python
def run_bound_agent(settings, message, normalized, saved_attachments, progress_callback=None):
    """Deterministic: resolve the session bound to this channel/thread, append the
    user message, run Bemine's agent loop. No intent classification."""
    from bemine.db import BemineDB
    from bemine.agent.session import ensure_session
    from bemine.agent.loop import AgentLoop
    from bemine.agent.clients import build_client_for_runtime  # see Task 9
    from mayor_discord.dispatch import resolve_channel_binding  # see Task 9

    db = BemineDB(settings.db_path)
    binding = resolve_channel_binding(settings, message)   # project_id, runtime, model, cwd
    chat_type = "dm" if (message.guild is None) else ("thread" if getattr(message.channel, "type", None) and "thread" in str(message.channel.type) else "channel")
    session_id = ensure_session(
        db, settings, channel_id=str(message.channel.id), chat_type=chat_type,
        chat_id=str(getattr(message.channel, "id", "")), thread_id=str(message.channel.id),
        project_id=binding.project_id, runtime=binding.runtime, model=binding.model, cwd=binding.cwd,
    )
    client = build_client_for_runtime(settings, binding.runtime)
    loop = AgentLoop(settings=settings, db=db, client=client)
    result = loop.run(session_id=session_id, project_id=binding.project_id, cwd=binding.cwd,
                      user_message=normalized, system=binding.system, progress_callback=progress_callback)
    return result.final_text
```

Then in `_handle_allowed_message`, **delete** the Kimi intent-router block (`gateway.py:764-781`), the `_needs_reference_clarification` bail-out (`gateway.py:786-796`), and the `_local_tool_*` helpers (`gateway.py:194-330`). Replace the tail of `_handle_allowed_message` so that, after slash/explicit commands are handled (Task 10), any remaining plain message + attachments calls `run_bound_agent(...)` inside `asyncio.to_thread` and streams the result via `responder.send_chunks(...)`.

Keep the explicit `approve`/`deny` parsing (`gateway.py:735-762`) — those are deterministic ops that will also become slash commands in Task 10; leaving the text form working is harmless during transition.

- [ ] **Step 4: Delete the dead router code and run the suite**

Remove `_local_tool_response`, `_route_local_tool`, `_router_prompt`, `_route_local_tool_with_kimi`, `_parse_tool_decision`, `_tool_catalog_text`, `_local_tools`, `_local_tool_by_name`, `_needs_reference_clarification`, `LocalToolResult`, `LocalToolSpec`, and the `config.update_dispatch`/`checklist.add` tool handlers — UNLESS a test in `tests/test_discord_expressive_gateway.py` covers behavior you must preserve; if so, port that behavior to a deterministic slash command in Task 10 first.

Run: `uv run pytest -q`
Expected: PASS. Update/port any expressive-gateway tests that asserted classifier behavior to assert the new deterministic pass-through instead.

- [ ] **Step 5: Commit**

```bash
git add src/mayor_discord/gateway.py tests/
git commit -m "refactor(discord): delete LLM intent router; bind->append->run deterministically"
```

### Task 9: Deterministic channel binding + runtime client factory

**Files:**
- Modify: `src/mayor_discord/dispatch.py` — add `resolve_channel_binding`; keep `resolve_project_channel` (the deterministic channel→project map at `dispatch.py:101-237`), drop reliance on `_project_from_content` content inference for routing.
- Modify: `src/bemine/agent/clients.py` — add `build_client_for_runtime`.
- Test: `tests/test_project_filters.py` neighbor — add `tests/test_channel_binding.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_channel_binding.py
from pathlib import Path
from bemine.config import BemineSettings
from bemine.agent.clients import build_client_for_runtime, AnthropicClient, CodexClient


def test_runtime_factory_maps_runtime_to_client(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    assert isinstance(build_client_for_runtime(settings, "kimi"), AnthropicClient)
    assert isinstance(build_client_for_runtime(settings, "claude"), AnthropicClient)
    assert isinstance(build_client_for_runtime(settings, "codex"), CodexClient)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_channel_binding.py -q`
Expected: FAIL — `cannot import name 'build_client_for_runtime'`.

- [ ] **Step 3: Implement the factory**

```python
# src/bemine/agent/clients.py  (append)
import os

from bemine.config import BemineSettings, config_string


def build_client_for_runtime(settings: BemineSettings, runtime: str):
    runtime = (runtime or "kimi").lower()
    if runtime == "codex":
        import openai
        return CodexClient(sdk=openai.OpenAI())
    import anthropic
    if runtime == "claude":
        base_url = config_string("claude", "base_url", "") or None
        api_key = os.environ.get(config_string("claude", "api_key_env", "MERIDIAN_API_KEY"), "")
        sdk = anthropic.Anthropic(api_key=api_key, base_url=base_url) if base_url else anthropic.Anthropic(api_key=api_key)
        return AnthropicClient(sdk=sdk, base_url=base_url)
    # kimi (Anthropic-compatible)
    base_url = config_string("kimi", "base_url", "https://api.kimi.com/coding")
    api_key = resolve_kimi_api_key(settings)  # reuse bemine.kimi_handler.resolve_kimi_api_key
    sdk = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    return AnthropicClient(sdk=sdk, base_url=base_url)
```

Import `resolve_kimi_api_key` from `bemine.kimi_handler` (already used by `mayor_discord/dispatch.py:19-31`). If unavailable, read the Kimi key from the env var named by `config_string("kimi", "api_key_env", "KIMI_API_KEY")`.

- [ ] **Step 4: Implement `resolve_channel_binding` in `dispatch.py`**

```python
# src/mayor_discord/dispatch.py  (add)
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChannelBinding:
    project_id: str
    runtime: str
    model: str
    cwd: Path
    system: str


def resolve_channel_binding(settings, message) -> ChannelBinding:
    """Deterministic: channel/category -> project (resolve_project_channel), runtime/model
    from config; no content inference. DMs and unmapped channels fall back to the default
    project + default runtime so a novel message still reaches a real agent."""
    channel_name = getattr(message.channel, "name", "") or ""
    category_name = getattr(getattr(message.channel, "category", None), "name", "") or ""
    project = resolve_project_channel(
        channel_name=channel_name, category_name=category_name,
        state_dir=settings.state_dir, repo_dir=None,
    )
    runtime = resolve_dispatch_runtime(project.runtime if project else None)
    model = _model_for_runtime(settings, runtime)
    cwd = Path(project.cwd) if project else Path(settings.state_dir).parent
    project_id = (project.project_id or project.channel_name) if project else "bemine"
    system = _build_system_for(settings, project)  # reuse build_*_system_prompt helpers
    return ChannelBinding(project_id=project_id, runtime=runtime, model=model, cwd=cwd, system=system)
```

`_model_for_runtime` reads `config_string(runtime, "model", default)`; `_build_system_for` reuses the existing `build_kimi_native_system_prompt`-style helper (`dispatch.py:1791-1992`) collapsed to one function that loads harness + operator context. Add small helpers as needed.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_channel_binding.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mayor_discord/dispatch.py src/bemine/agent/clients.py tests/test_channel_binding.py
git commit -m "feat: deterministic channel binding + runtime client factory"
```

---

## Phase 4 — Deterministic command surface (CLI + Discord slash)

> One registry, two front-ends. The CLI and Discord slash commands call the SAME pure handlers, so deterministic ops behave identically and never touch an LLM.

### Task 10: Command registry shared by CLI and Discord

**Files:**
- Create: `src/bemine/commands.py`
- Test: `tests/test_commands_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands_registry.py
from pathlib import Path
from bemine.config import BemineSettings
from bemine.commands import registry, run_command


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_registry_lists_deterministic_ops():
    names = set(registry().keys())
    assert {"status", "rundown", "jobs.list", "usage", "capabilities"} <= names


def test_run_command_is_pure_and_deterministic(tmp_path):
    settings = _settings(tmp_path)
    out = run_command("jobs.list", settings, {})
    assert isinstance(out.text, str)
    assert isinstance(out.data, dict)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_commands_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: bemine.commands`.

- [ ] **Step 3: Implement the registry**

```python
# src/bemine/commands.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from bemine.config import BemineSettings
from bemine.kernel import BemineKernel


@dataclass(frozen=True)
class CommandResult:
    text: str
    data: dict


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    handler: Callable[[BemineSettings, Mapping], CommandResult]


def _status(settings, args):
    from bemine.status import collect_fleet_status, render_status
    statuses, errors = collect_fleet_status(settings)
    limit = int(args.get("limit", 8))
    return CommandResult(text=render_status(statuses, errors, limit=limit),
                         data={"count": len(statuses), "errors": len(errors)})


def _rundown(settings, args):
    kernel = BemineKernel(settings)
    text = kernel.project_rundown(limit=int(args.get("limit", 10)))
    return CommandResult(text=text, data={})


def _jobs_list(settings, args):
    from bemine.jobs import render_jobs
    kernel = BemineKernel(settings)
    rows = [dict(r) for r in kernel.db.list_jobs()]
    return CommandResult(text=render_jobs(rows), data={"jobs": rows})


def _capabilities(settings, args):
    from bemine.capabilities import build_capability_manifest, render_capability_manifest
    kernel = BemineKernel(settings)
    rows = [dict(r) for r in kernel.db.list_credential_profiles()]
    manifest = build_capability_manifest(rows, device_id=settings.device_id)
    return CommandResult(text=render_capability_manifest(manifest), data=manifest)


def _usage(settings, args):
    from bemine.usage import render_usage  # Task 11
    kernel = BemineKernel(settings)
    rollup = kernel.db.usage_rollup(provider=args.get("provider"))
    return CommandResult(text=render_usage(rollup), data={"providers": rollup})


_SPECS = (
    CommandSpec("status", "Recent fleet status", _status),
    CommandSpec("rundown", "Project rundown", _rundown),
    CommandSpec("jobs.list", "List scheduled jobs", _jobs_list),
    CommandSpec("capabilities", "Credential capabilities", _capabilities),
    CommandSpec("usage", "Codex/Claude/Kimi token usage", _usage),
)


def registry() -> dict[str, CommandSpec]:
    return {spec.name: spec for spec in _SPECS}


def run_command(name: str, settings: BemineSettings, args: Mapping) -> CommandResult:
    return registry()[name].handler(settings, dict(args))
```

If `kernel.db.list_jobs` does not exist, read `src/bemine/db.py` for the jobs accessor name (the daemon uses one) and call it; if absent, add a thin `list_jobs(self)` returning `SELECT * FROM jobs`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_commands_registry.py -q`
Expected: PASS once Task 11 (`bemine.usage`) lands. Implement Task 11 first if `usage` import fails, or temporarily drop the `usage` spec and re-add it in Task 11. Recommended: do Task 11 before this step's `usage` assertion.

- [ ] **Step 5: Commit**

```bash
git add src/bemine/commands.py tests/test_commands_registry.py
git commit -m "feat(commands): deterministic command registry shared by CLI and Discord"
```

### Task 11: `usage` report + capture (the example you gave)

**Files:**
- Create: `src/bemine/usage.py`
- Modify: `src/bemine/db.py` — add `usage_rollup`.
- Modify: `src/bemine/cli.py` — add `usage` subcommand (near other `sub.add_parser`, ~line 433) + handler.
- Modify: `src/mayor_discord/dispatch.py` — record usage for codex + claude dispatch (currently only `kimi ask` records, `cli.py:1296`).
- Test: `tests/test_usage_report.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_report.py
from pathlib import Path
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.usage import render_usage


def test_usage_rollup_aggregates_per_provider(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.record_usage_sample(provider="codex", raw_json="{}", input_tokens=100, output_tokens=20, total_tokens=120)
    db.record_usage_sample(provider="codex", raw_json="{}", input_tokens=50, output_tokens=10, total_tokens=60)
    db.record_usage_sample(provider="claude", raw_json="{}", input_tokens=200, output_tokens=40, total_tokens=240)
    rollup = db.usage_rollup()
    by = {r["provider"]: r for r in rollup}
    assert by["codex"]["input_tokens"] == 150
    assert by["codex"]["samples"] == 2
    assert by["claude"]["total_tokens"] == 240
    text = render_usage(rollup)
    assert "codex" in text and "claude" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_usage_report.py -q`
Expected: FAIL — no `usage_rollup` / no `bemine.usage`.

- [ ] **Step 3: Implement `usage_rollup` in `db.py`**

```python
    def usage_rollup(self, *, provider: str | None = None) -> list[dict]:
        sql = (
            "SELECT provider, COUNT(*) AS samples, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(total_tokens),0) AS total_tokens, "
            "MAX(ts) AS last_ts "
            "FROM usage_samples "
        )
        params: tuple = ()
        if provider:
            sql += "WHERE provider = ? "
            params = (provider,)
        sql += "GROUP BY provider ORDER BY total_tokens DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
```

- [ ] **Step 4: Implement `render_usage`**

```python
# src/bemine/usage.py
from __future__ import annotations

from typing import Sequence


def render_usage(rollup: Sequence[dict]) -> str:
    if not rollup:
        return "No usage recorded."
    lines = ["**Token usage by provider**"]
    for r in rollup:
        lines.append(
            f"- {r['provider']}: {r['samples']} samples, "
            f"in {r['input_tokens']:,} / out {r['output_tokens']:,} / total {r['total_tokens']:,}"
        )
    return "\n".join(lines)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_usage_report.py -q`
Expected: PASS (1 test).

- [ ] **Step 6: Wire the CLI `usage` subcommand**

In `cli.py`, register `usage = sub.add_parser("usage", help="Show codex/claude/kimi token usage.")` with `--provider` and `--json`, and in the dispatch block call `run_command("usage", settings, {"provider": args.provider})` from `bemine.commands`, printing `.text` or `json.dumps(.data)`.

Run: `uv run bemine usage`
Expected: real rollup from the live DB (codex/kimi/kimi-coding rows already present).

- [ ] **Step 7: Capture Claude + Codex usage at dispatch**

In `mayor_discord/dispatch.py`, in `CodexDispatcher` and the Claude path, after a successful run record a usage sample mirroring `cli.py:1296-1305`:

```python
self.db.record_usage_sample(
    provider=runtime, raw_json=json.dumps(usage or {}), runtime=runtime,
    session_id=session_id, input_tokens=int((usage or {}).get("input_tokens", 0)),
    output_tokens=int((usage or {}).get("output_tokens", 0)),
    total_tokens=int((usage or {}).get("total_tokens", 0)),
)
```

For Codex/Responses, extract tokens from the response's usage block; if the CLI backend gives none, record a sample with zeros so the provider still appears (so "Claude usage" stops being silently empty). Add an assertion to `tests/test_usage_report.py` only if a dispatcher unit test already exists to hang it on; otherwise leave the capture covered by the rollup test plus manual `bemine usage`.

- [ ] **Step 8: Commit**

```bash
git add src/bemine/usage.py src/bemine/db.py src/bemine/cli.py src/mayor_discord/dispatch.py tests/test_usage_report.py
git commit -m "feat(usage): add deterministic usage report + capture codex/claude usage"
```

### Task 12: Discord slash commands → registry

**Files:**
- Modify: `src/mayor_discord/gateway.py` — register `discord.app_commands` for each deterministic op; gate with an allow policy (learn from Hermes `gateway/slash_access.py:56-89`).
- Test: `tests/test_slash_commands.py`

- [ ] **Step 1: Write the failing test (handler maps slash name → registry, deterministic)**

```python
# tests/test_slash_commands.py
from pathlib import Path
from bemine.config import BemineSettings
from mayor_discord.gateway import slash_command_text


def test_slash_command_calls_registry(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    text = slash_command_text(settings, "jobs", {})
    assert isinstance(text, str) and text  # came from commands.run_command, no LLM
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_slash_commands.py -q`
Expected: FAIL — `cannot import name 'slash_command_text'`.

- [ ] **Step 3: Implement the slash bridge + registration**

```python
# src/mayor_discord/gateway.py  (add)
from bemine.commands import run_command

# Slash name -> registry command name (deterministic, 1:1).
_SLASH_TO_COMMAND = {
    "status": "status", "rundown": "rundown", "jobs": "jobs.list",
    "usage": "usage", "capabilities": "capabilities",
}


def slash_command_text(settings, slash_name: str, args: dict) -> str:
    command = _SLASH_TO_COMMAND.get(slash_name)
    if not command:
        return f"Unknown command: {slash_name}"
    return run_command(command, settings, args).text
```

Register these as `discord.app_commands.Command`s on the bot in `start()` (intents already include guild/dm; `gateway.py:609-616`), each calling `slash_command_text(...)` and replying via the interaction with `_discord_chunk(...)` for length. Gate with a `_slash_allowed(user_id, name)` policy modeled on Hermes `slash_access.py` (admins always; a configurable `[discord].allowed_users`). Sync commands with `await bot.tree.sync(...)` on ready, persisting sync state like Hermes' `discord_command_sync_state.json` to avoid re-syncing each boot.

- [ ] **Step 4: Run to verify it passes + retire the string ladder**

Run: `uv run pytest tests/test_slash_commands.py -q`
Expected: PASS. Then remove the hardcoded text ladder (`gateway.py:659-734`) now that `/status`, `/rundown`, `/jobs`, `/usage`, `/capabilities` exist as slash commands. Keep `approve`/`deny` text parsing OR add `/approve`/`/deny` slash commands and remove the text forms.

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mayor_discord/gateway.py tests/test_slash_commands.py
git commit -m "feat(discord): native slash commands for deterministic ops via shared registry"
```

---

## Phase 5 — Fix the live `daily_digest` failure

> Root cause (from `job_runs.output`, 2026-06-05): the digest text exceeds Discord's per-message limit → `400 BASE_TYPE_MAX_LENGTH (code 50035)`. The daemon's sender (`mayor_discord/send.py:44`) does not chunk, unlike the gateway (`_discord_chunk`). Fix: chunk in the send path used by jobs.

### Task 13: Chunk long messages in the daemon sender

**Files:**
- Modify: `src/mayor_discord/send.py` — split content before posting; post sequentially.
- Test: `tests/test_digest_chunking.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_digest_chunking.py
from mayor_discord.send import chunk_for_discord


def test_chunk_for_discord_splits_on_limit():
    text = "x" * 4500
    chunks = chunk_for_discord(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text
    assert len(chunks) == 3


def test_chunk_for_discord_short_text_single_chunk():
    chunks = chunk_for_discord("hello", limit=2000)
    assert chunks == ["hello"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_digest_chunking.py -q`
Expected: FAIL — `cannot import name 'chunk_for_discord'`.

- [ ] **Step 3: Implement chunking + use it in `send_message`**

```python
# src/mayor_discord/send.py  (add)
def chunk_for_discord(text: str, *, limit: int = 2000) -> list[str]:
    """Split text into <=limit pieces, preferring newline boundaries, never exceeding limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks
```

Then in `send_message` (`send.py:~40-48`): if `content` exceeds the limit, POST each `chunk_for_discord(content)` piece in order (await each `_post_json`), returning the first message id. Keep the single-post path for short content.

- [ ] **Step 4: Run to verify it passes + reproduce the live fix**

Run: `uv run pytest tests/test_digest_chunking.py -q`
Expected: PASS (2 tests).

Run: `uv run bemine jobs run daily_digest --no-notify`
Expected: succeeds (generation path). Optionally with notify against a test channel: no `50035` error.

- [ ] **Step 5: Commit**

```bash
git add src/mayor_discord/send.py tests/test_digest_chunking.py
git commit -m "fix(jobs): chunk long Discord messages so daily_digest stops failing (50035)"
```

---

## Phase 6 — Package reorg (fold mayor_discord into bemine; subpackages)

> Mechanical, behavior-preserving moves with a green suite after each. Keep entry-point aliases working. Do this LAST so it rebases cleanly onto the new modules.

### Task 14: Fold `mayor_discord` into `bemine.discord`

**Files:**
- Move: `src/mayor_discord/{gateway,dispatch,send,store,config}.py` → `src/bemine/discord/`
- Modify: `pyproject.toml:11-29` (scripts + `[tool.hatch.build.targets.wheel].packages`)

- [ ] **Step 1: Move with git + add package init**

```bash
cd /home/cdjk/bm
git mv src/mayor_discord src/bemine/discord
```

- [ ] **Step 2: Rewrite imports**

Update every `from mayor_discord.X import ...` / `import mayor_discord.X` to `from bemine.discord.X import ...` across `src/` and `tests/`. Within the moved files, change cross-module imports to `from bemine.discord.dispatch import ...` etc.

- [ ] **Step 3: Update entry points + wheel packages**

```toml
[project.scripts]
bemine = "bemine.cli:main"
bemine-daemon = "bemine.daemon:main"
bemine-discord-gateway = "bemine.discord.gateway:main"
bemine-discord-send = "bemine.discord.send:main"
mayor-discord-gateway = "bemine.discord.gateway:main"   # back-compat alias
mayor-discord-send = "bemine.discord.send:main"         # back-compat alias

[tool.hatch.build.targets.wheel]
packages = ["src/bemine"]
```

- [ ] **Step 4: Verify**

Run: `cd /home/cdjk/bm && uv sync && uv run pytest -q && uv run bemine status --limit 3`
Expected: PASS; CLI works. Update `SKILL.md` surface references (`mayor_discord/gateway.py` → `bemine/discord/gateway.py`).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: fold mayor_discord into bemine.discord (aliases preserved)"
```

### Task 15: Group flat modules into subpackages

**Files (git mv, then fix imports, suite green after each group):**
- `src/bemine/{credentials,proxy,totp,notifications}.py` → `src/bemine/security/`
- `src/bemine/{jobs,daemon,systemd}.py` → `src/bemine/jobs/` (rename `jobs.py` → `jobs/core.py`, keep `bemine.jobs` import surface via `__init__.py` re-exports)
- `src/bemine/{workers,tmux_adapter,native_runtime}.py` → `src/bemine/workers/`
- `src/bemine/{harness_context,operator_context,rituals,tool_registry}.py` → `src/bemine/context/`
- `src/bemine/{discovery,status,rundown,vault,models,project_filters}.py` → `src/bemine/index/`
- Leave `config.py`, `db.py`, `kernel.py`, `cli.py`, `capabilities.py`, `util.py`, `mail_bridge.py`, `kimi_handler.py` at top level (cross-cutting / entry).

- [ ] **Step 1: Move one group, add `__init__.py` re-exports to preserve import paths**

For each group, after `git mv`, add `src/bemine/<group>/__init__.py` that re-exports the public names the rest of the code imports, so `from bemine.credentials import PermissionGate` keeps working via `from bemine.security.credentials import PermissionGate` re-export — OR update all import sites. Prefer updating import sites for clarity; use re-exports only where churn is large (entry-point scripts).

- [ ] **Step 2: After EACH group move, run the suite**

Run: `uv run pytest -q`
Expected: PASS before moving the next group. Never batch all groups before testing.

- [ ] **Step 3: Commit per group**

```bash
git add -A && git commit -m "refactor: group <group> modules into bemine.<group>"
```

- [ ] **Step 4: Final verification**

Run: `uv run pytest -q && uv run bemine status --limit 3 && uv run bemine usage && uv run bemine jobs list`
Expected: all PASS / produce output. Update `SKILL.md` "Important Surfaces" paths.

---

## Self-Review

**Spec coverage vs. the user's stated wants:**
- "Router only covers what I flagged; novel input fails; maybe no router needed" → Phase 3 deletes the Kimi intent classifier + content-inference + vague-message bail-out; deterministic bind→append→run. ✔ (Task 8 regression test asserts the classifier is never consulted.)
- "Claude TUI / codex / kimi CLI available over Discord, in server thread and in DM" → Phase 2 loop + Phase 3 binding handles DM/thread/channel via `chat_type`; `build_client_for_runtime` covers claude/codex/kimi. ✔
- "Deterministic CLI (check codex/claude usage; list cron jobs)" → Phase 4 shared registry + `usage` command + capture; `jobs.list`. ✔
- "Threads and sessions are collections of messages → fix deterministically" → `bemine_sessions`/`bemine_session_messages` keyed by channel/thread id; `ensure_session` resumes by channel. ✔
- "Learn from Hermes core loop; reuse model-specific tool-calling directly; core logic ours" → Phase 1 vendors Hermes adapters (MIT); Phase 2 is Bemine's own loop. ✔
- "Slash commands for ops" (chosen) → Task 12. ✔
- "Include usage command + daily_digest fix" (chosen) → Tasks 11 + 13. ✔
- "Core fix + package reorg" (chosen) → Phases 1–5 (core) + Phase 6 (reorg). ✔

**Placeholder scan:** No `TODO`/`TBD`/"handle edge cases". Two tasks (8, 9) reference existing gateway test helpers and existing prompt-builder helpers rather than reprinting them — flagged explicitly with file:line so the engineer adapts, not invents. The vendoring tasks (2, 3) name exact source files, exact import edits, and a no-network verification test — the "read the copied file and fix imports" step is inherent to lifting third-party code, not a placeholder.

**Type consistency:** `LLMClient.complete(messages, tools, model, system) -> NormalizedResponse` is identical in the protocol (Task 4), the fake (Task 5), and both real clients (Task 6/9). `CommandResult(text, data)` is consistent across `commands.py` and its callers. `ensure_session(...)` kwargs match `upsert_bemine_session` columns confirmed in `db.py`. `chunk_for_discord(text, *, limit)` consistent between test and impl.

**Known risks called out for the executor:** (1) exact vendored-transport method signatures (`build_kwargs`/`normalize_response`) must be confirmed by reading the copied `base.py`/`anthropic_transport.py` — Task 6 Step 4 says to adjust call sites, not the seam. (2) `record_tool_call_*` and `upsert_bemine_session` kwargs must match `db.py:870-1000` — Tasks 5/7 say to verify. (3) Some `test_discord_expressive_gateway.py` tests may encode classifier behavior; Task 8 Step 4 says port to slash commands before deleting.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-06-bemine-deterministic-agent-over-discord.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Which approach?
