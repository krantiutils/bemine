# Watcher Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-project autonomous watcher that drives a tmux-hosted agent session toward a user-supplied context brief — polling at an interval, deciding keystrokes via a sub-agent when the session waits on input, sending them over SSH for remote devices, escalating to the project channel only when blocked, and echoing every action.

**Architecture:** A new `project_watcher` job, distinct from the notify-only `session_watcher`, wired into the existing `BemineDaemon` poll loop. State lives in a new `project_watchers` table. Each tick: resolve the project's live session from fleet status, find its tmux pane (locally or via `ssh <device> tmux …`), capture it, and — if the session is waiting on input — ask a one-shot decision sub-agent what to do, then send keys / escalate / stop. Control is plain-language in the project channel (a `watch_control` tool on the existing channel agent) plus a `bemine watch` CLI. Spec: `docs/superpowers/specs/2026-06-08-watcher-mode-design.md`.

**Tech Stack:** Python 3, SQLite (stdlib `sqlite3`), `subprocess` for tmux/ssh, the existing vendored LLM clients (`bemine.agent.clients`), `aiohttp` (via `bemine.discord.send`), `pytest`, `uv run`.

---

## Conventions (read once)

- Run tests with `uv run pytest <path> -v`. Run all with `uv run pytest -q`.
- Lint changed files with `uv run ruff check <paths>`.
- The DB schema is a single `SCHEMA` string in `src/bemine/db.py` executed by `BemineDB.init()` with `CREATE TABLE IF NOT EXISTS`. Add the new table by appending to that string; `init()` runs on every `BemineDB(...)`.
- `BemineDB.connect()` yields a `sqlite3.Connection` with `row_factory = sqlite3.Row`. Methods use `with self.connect() as conn:`.
- Time is `time.time()` (float epoch). `db.py` already `import time`.
- Tests construct settings with `BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")` and `BemineDB(settings.db_path)`.

---

## Task 1: `project_watchers` table + DB methods

**Files:**
- Modify: `src/bemine/db.py` (append to `SCHEMA`; add methods on `BemineDB`)
- Test: `tests/test_project_watcher_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_project_watcher_store.py
from bemine.config import BemineSettings
from bemine.db import BemineDB


def _db(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    return BemineDB(settings.db_path)


def test_upsert_and_get_project_watcher(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(
        project_id="p1", channel_id="c1", brief="long brief", interval_secs=600
    )
    row = db.get_project_watcher("p1")
    assert row["channel_id"] == "c1"
    assert row["brief"] == "long brief"
    assert row["interval_secs"] == 600
    assert row["status"] == "active"
    assert row["awaiting_reply"] == 0
    assert row["last_tick_at"] == 0


def test_upsert_is_idempotent_and_updates_brief(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="a", interval_secs=300)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="b", interval_secs=120)
    row = db.get_project_watcher("p1")
    assert row["brief"] == "b"
    assert row["interval_secs"] == 120
    assert db.get_project_watcher("p1")["status"] == "active"


def test_get_by_channel_and_list_active(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="a", interval_secs=300)
    db.upsert_project_watcher(project_id="p2", channel_id="c2", brief="b", interval_secs=300)
    db.update_project_watcher("p2", status="stopped")
    assert db.get_project_watcher_by_channel("c1")["project_id"] == "p1"
    active = db.list_active_project_watchers()
    assert [r["project_id"] for r in active] == ["p1"]


def test_update_fields(tmp_path):
    db = _db(tmp_path)
    db.upsert_project_watcher(project_id="p1", channel_id="c1", brief="a", interval_secs=300)
    db.update_project_watcher("p1", awaiting_reply=1, last_tick_at=12.5, empty_ticks=2)
    row = db.get_project_watcher("p1")
    assert row["awaiting_reply"] == 1
    assert row["last_tick_at"] == 12.5
    assert row["empty_ticks"] == 2


def test_channel_id_for_project(tmp_path):
    db = _db(tmp_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c9", project_id="p1", runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    assert db.get_channel_id_for_project("p1") == "c9"
    assert db.get_channel_id_for_project("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_project_watcher_store.py -v`
Expected: FAIL — `AttributeError: 'BemineDB' object has no attribute 'upsert_project_watcher'`.

- [ ] **Step 3: Append the table to `SCHEMA`**

In `src/bemine/db.py`, immediately before the closing `"""` of the `SCHEMA` string (after the `session_fts` virtual table), add:

```sql

CREATE TABLE IF NOT EXISTS project_watchers (
    project_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    brief TEXT NOT NULL,
    interval_secs INTEGER NOT NULL,
    status TEXT NOT NULL,
    last_tick_at REAL NOT NULL DEFAULT 0,
    awaiting_reply INTEGER NOT NULL DEFAULT 0,
    empty_ticks INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    created_at REAL NOT NULL
);
```

- [ ] **Step 4: Add the methods**

Add to the `BemineDB` class in `src/bemine/db.py` (place after the `bemine_sessions` methods, e.g. after `get_bemine_session_by_channel`):

```python
    def upsert_project_watcher(self, *, project_id: str, channel_id: str,
                               brief: str, interval_secs: int) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO project_watchers(
                    project_id, channel_id, brief, interval_secs, status,
                    last_tick_at, awaiting_reply, empty_ticks, started_at, created_at
                ) VALUES(?, ?, ?, ?, 'active', 0, 0, 0, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    brief=excluded.brief,
                    interval_secs=excluded.interval_secs,
                    status='active',
                    awaiting_reply=0,
                    empty_ticks=0,
                    started_at=excluded.started_at
                """,
                (project_id, channel_id, brief, int(interval_secs), now, now),
            )

    def get_project_watcher(self, project_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_watchers WHERE project_id = ?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_project_watcher_by_channel(self, channel_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_watchers WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_active_project_watchers(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_watchers WHERE status = 'active' "
                "ORDER BY last_tick_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_project_watcher(self, project_id: str, **fields) -> None:
        allowed = {"channel_id", "brief", "interval_secs", "status",
                   "last_tick_at", "awaiting_reply", "empty_ticks"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE project_watchers SET {assignments} WHERE project_id = ?",
                (*sets.values(), project_id),
            )

    def get_channel_id_for_project(self, project_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT channel_id FROM bemine_sessions WHERE project_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return str(row["channel_id"]) if row else None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_project_watcher_store.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add src/bemine/db.py tests/test_project_watcher_store.py
git commit -m "feat(watcher): project_watchers table + store methods"
```

---

## Task 2: tmux/SSH runner

A thin wrapper that runs `tmux` locally for the gateway device and `ssh <device> tmux …` otherwise. The subprocess runner is injectable so tests never shell out.

**Files:**
- Create: `src/bemine/jobs/remote_tmux.py`
- Test: `tests/test_remote_tmux.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_remote_tmux.py
import subprocess
from bemine.jobs.remote_tmux import RemoteTmux


class _FakeRunner:
    def __init__(self, stdout=""):
        self.calls = []
        self._stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=self._stdout, stderr="")


def test_local_device_runs_tmux_directly():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_keys("cdjk", "%1", ["Down", "Enter"])
    assert runner.calls[-1][:1] == ["tmux"]
    assert runner.calls[-1] == ["tmux", "send-keys", "-t", "%1", "Down", "Enter"]


def test_remote_device_wraps_in_ssh():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_keys("mbp", "%2", ["Enter"])
    assert runner.calls[-1] == ["ssh", "mbp", "tmux", "send-keys", "-t", "%2", "Enter"]


def test_send_text_is_literal_then_enter():
    runner = _FakeRunner()
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    rt.send_text("mbp", "%2", "hello world")
    assert runner.calls[-2] == ["ssh", "mbp", "tmux", "send-keys", "-t", "%2", "-l", "hello world"]
    assert runner.calls[-1] == ["ssh", "mbp", "tmux", "send-keys", "-t", "%2", "Enter"]


def test_list_panes_parses_tab_separated_output():
    out = "%1\t/Users/cdjk/github/llm/rai-speech-toolkit\tclaude\n%2\t/home/cdjk\tbash\n"
    runner = _FakeRunner(stdout=out)
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    panes = rt.list_panes("mbp")
    assert panes[0] == {"pane_id": "%1",
                        "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                        "command": "claude"}
    assert runner.calls[-1][:3] == ["ssh", "mbp", "tmux"]
    assert "list-panes" in runner.calls[-1]


def test_capture_returns_stdout():
    runner = _FakeRunner(stdout="pane contents\n")
    rt = RemoteTmux(gateway_device="cdjk", runner=runner)
    assert rt.capture("cdjk", "%1") == "pane contents\n"
    assert runner.calls[-1] == ["tmux", "capture-pane", "-p", "-t", "%1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_remote_tmux.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bemine.jobs.remote_tmux'`.

- [ ] **Step 3: Write the implementation**

```python
# src/bemine/jobs/remote_tmux.py
from __future__ import annotations

import subprocess
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class RemoteTmuxError(RuntimeError):
    pass


class RemoteTmux:
    """Run tmux commands locally (gateway device) or over SSH (any other device).

    The subprocess runner is injectable so callers and tests can substitute it.
    """

    def __init__(self, *, gateway_device: str, runner: Runner | None = None,
                 timeout: int = 15) -> None:
        self.gateway_device = gateway_device
        self._runner = runner or self._default_runner
        self.timeout = timeout

    def _default_runner(self, argv, **kwargs):
        return subprocess.run(
            argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=self.timeout, check=False, **kwargs
        )

    def _argv(self, device: str, tmux_args: list[str]) -> list[str]:
        if device == self.gateway_device or device == "local":
            return ["tmux", *tmux_args]
        return ["ssh", device, "tmux", *tmux_args]

    def _run(self, device: str, tmux_args: list[str]) -> str:
        argv = self._argv(device, tmux_args)
        proc = self._runner(argv)
        if proc.returncode != 0:
            raise RemoteTmuxError(
                (proc.stderr or "").strip() or f"tmux failed: {' '.join(argv)}"
            )
        return proc.stdout or ""

    def list_panes(self, device: str) -> list[dict]:
        out = self._run(device, [
            "list-panes", "-a", "-F",
            "#{pane_id}\t#{pane_current_path}\t#{pane_current_command}",
        ])
        panes: list[dict] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            panes.append({"pane_id": parts[0], "path": parts[1], "command": parts[2]})
        return panes

    def capture(self, device: str, pane_id: str) -> str:
        return self._run(device, ["capture-pane", "-p", "-t", pane_id])

    def send_keys(self, device: str, pane_id: str, keys: list[str]) -> None:
        self._run(device, ["send-keys", "-t", pane_id, *keys])

    def send_text(self, device: str, pane_id: str, text: str) -> None:
        # -l sends text literally (no key-name interpretation), then a separate Enter.
        self._run(device, ["send-keys", "-t", pane_id, "-l", text])
        self._run(device, ["send-keys", "-t", pane_id, "Enter"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_remote_tmux.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/remote_tmux.py tests/test_remote_tmux.py
git commit -m "feat(watcher): RemoteTmux runner (local + ssh)"
```

---

## Task 3: pane resolver + project session resolver

Map a project to its live session (from fleet status) and that session to a tmux pane.

**Files:**
- Create: `src/bemine/jobs/project_watcher.py` (resolver functions; tick logic added in Task 6)
- Test: `tests/test_watcher_resolve.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watcher_resolve.py
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import resolve_project_session, resolve_pane


def _status(cwd, age, device="mbp"):
    return SessionStatus(
        device_id=device, runtime="claude", session_id="s", cwd=cwd,
        source_path="", last_interaction_at=1000 - age, age_seconds=age,
        last_role="assistant", asks_user=True, ask_summary="", latest_summary="",
    )


def test_resolve_project_session_matches_by_cwd_basename_most_recent():
    statuses = [
        _status("/Users/cdjk/github/llm/rai-speech-toolkit", age=500),
        _status("/Users/cdjk/github/llm/rai-speech-toolkit", age=30),
        _status("/Users/cdjk/other", age=10),
    ]
    found = resolve_project_session(statuses, "rai-speech-toolkit")
    assert found is not None
    assert found.age_seconds == 30  # most recent of the matching ones


def test_resolve_project_session_none_when_no_match():
    assert resolve_project_session([_status("/x/y/z", age=1)], "nope") is None


class _FakeTmux:
    def __init__(self, panes):
        self._panes = panes
    def list_panes(self, device):
        return self._panes


def test_resolve_pane_matches_path_and_agent_command():
    tmux = _FakeTmux([
        {"pane_id": "%1", "path": "/home/cdjk", "command": "bash"},
        {"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit", "command": "claude"},
    ])
    assert resolve_pane(tmux, "mbp", "/Users/cdjk/github/llm/rai-speech-toolkit") == "%2"


def test_resolve_pane_none_when_no_path_match():
    tmux = _FakeTmux([{"pane_id": "%1", "path": "/home/cdjk", "command": "bash"}])
    assert resolve_pane(tmux, "mbp", "/Users/cdjk/github/llm/rai-speech-toolkit") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watcher_resolve.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bemine.jobs.project_watcher'`.

- [ ] **Step 3: Write the implementation**

```python
# src/bemine/jobs/project_watcher.py
from __future__ import annotations

from bemine.index.status import SessionStatus

# tmux pane commands that indicate an interactive agent session.
_AGENT_COMMANDS = {"claude", "codex", "kimi", "node", "python", "python3"}


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def resolve_project_session(statuses: list[SessionStatus],
                            project_name: str) -> SessionStatus | None:
    """The most-recently-active fleet session whose cwd basename matches the project."""
    matches = [s for s in statuses if _basename(s.cwd) == project_name]
    if not matches:
        return None
    return min(matches, key=lambda s: s.age_seconds)


def resolve_pane(tmux, device: str, cwd: str) -> str | None:
    """Find the tmux pane on `device` whose current path matches the session cwd.

    Prefers a pane also running an agent command, but falls back to any path match.
    """
    panes = tmux.list_panes(device)
    target = cwd.rstrip("/")
    path_matches = [p for p in panes if p.get("path", "").rstrip("/") == target]
    if not path_matches:
        return None
    for pane in path_matches:
        if pane.get("command", "") in _AGENT_COMMANDS:
            return pane["pane_id"]
    return path_matches[0]["pane_id"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watcher_resolve.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/project_watcher.py tests/test_watcher_resolve.py
git commit -m "feat(watcher): project-session and tmux-pane resolvers"
```

---

## Task 4: decision sub-agent

One-shot LLM call: given the brief + pane capture + transcript tail + ask summary, return a structured decision. The LLM client is injectable. The model returns a JSON object; we parse it defensively.

**Files:**
- Modify: `src/bemine/jobs/project_watcher.py` (add `Decision`, `decide`, `_parse_decision`)
- Test: `tests/test_watcher_decide.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watcher_decide.py
from bemine.jobs.project_watcher import decide, _parse_decision


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None
    def complete(self, *, messages, tools, model, system):
        self.last_kwargs = {"messages": messages, "tools": tools,
                            "model": model, "system": system}
        return _Resp(self._content)


def test_parse_send_text_decision():
    d = _parse_decision('{"kind": "send_text", "text": "yes, proceed"}')
    assert d.kind == "send_text"
    assert d.text == "yes, proceed"


def test_parse_send_keys_decision():
    d = _parse_decision('{"kind": "send_keys", "keys": ["Down", "Enter"]}')
    assert d.kind == "send_keys"
    assert d.keys == ["Down", "Enter"]


def test_parse_escalate_decision():
    d = _parse_decision('{"kind": "escalate", "question": "Which AWS account?"}')
    assert d.kind == "escalate"
    assert d.question == "Which AWS account?"


def test_parse_handles_fenced_json_and_garbage():
    d = _parse_decision('here you go:\n```json\n{"kind": "done", "rationale": "goal met"}\n```')
    assert d.kind == "done"
    assert d.rationale == "goal met"
    # Unparseable -> safe escalate
    bad = _parse_decision("I am not json")
    assert bad.kind == "escalate"


def test_decide_passes_brief_and_pane_to_client():
    client = _FakeClient('{"kind": "send_text", "text": "go"}')
    d = decide(client, model="kimi-k2.6", brief="get training running",
               pane_capture="Proceed? (y/n)", transcript_tail="...waiting...",
               ask_summary="asks to proceed", channel_context="")
    assert d.kind == "send_text"
    sent = client.last_kwargs
    assert "get training running" in sent["system"]
    assert "Proceed? (y/n)" in sent["messages"][-1]["content"]
    assert sent["tools"] == []  # one-shot, no tools
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watcher_decide.py -v`
Expected: FAIL — `ImportError: cannot import name 'decide'`.

- [ ] **Step 3: Write the implementation**

Add to `src/bemine/jobs/project_watcher.py`:

```python
import json
import re
from dataclasses import dataclass, field


@dataclass
class Decision:
    kind: str  # "send_text" | "send_keys" | "escalate" | "done" | "wait"
    text: str = ""
    keys: list[str] = field(default_factory=list)
    question: str = ""
    rationale: str = ""


_DECISION_SYSTEM = """You drive a stuck terminal agent session toward a goal.

You are given a CONTEXT BRIEF (the goal + background), the current rendered terminal
pane, and the tail of the session transcript. The session is waiting for input. Decide
the single next action and reply with ONE JSON object, nothing else:

- {"kind": "send_text", "text": "<exact text to type>"}  -- free-text answer/instruction
- {"kind": "send_keys", "keys": ["Down", "Down", "Enter"]}  -- navigate a menu/checkbox
    (use tmux key names: Up, Down, Left, Right, Space, Enter, BTab, or a digit like "2")
- {"kind": "escalate", "question": "<what you need from the human>"}  -- you are blocked
- {"kind": "done", "rationale": "<why the goal is achieved>"}  -- nothing left to do

Rules:
- Stay strictly within the CONTEXT BRIEF. Do not invent new goals.
- ESCALATE (never guess) when the prompt needs a credential, a real human decision, or
  any destructive/irreversible action (deleting data, force-push, tearing down infra).
- For a yes/continue/confirm prompt that clearly advances the goal, send the answer.
- Read the pane to choose the right keystrokes for menus/checkboxes."""


def _parse_decision(content: str | None) -> Decision:
    text = content or ""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return Decision(kind="escalate",
                        question="Could not parse a decision; please advise.")
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return Decision(kind="escalate",
                        question="Could not parse a decision; please advise.")
    kind = str(data.get("kind") or "escalate")
    return Decision(
        kind=kind,
        text=str(data.get("text") or ""),
        keys=[str(k) for k in (data.get("keys") or [])],
        question=str(data.get("question") or ""),
        rationale=str(data.get("rationale") or ""),
    )


def decide(client, *, model: str, brief: str, pane_capture: str,
           transcript_tail: str, ask_summary: str, channel_context: str) -> Decision:
    system = _DECISION_SYSTEM + "\n\nCONTEXT BRIEF:\n" + brief
    user = "\n".join([
        "Current terminal pane:",
        "----",
        pane_capture[-4000:],
        "----",
        "",
        "Transcript tail:",
        transcript_tail[-2000:],
        "",
        f"Detected ask: {ask_summary}",
        f"Recent channel messages:\n{channel_context}" if channel_context else "",
        "",
        "Reply with one JSON decision object.",
    ])
    resp = client.complete(
        messages=[{"role": "user", "content": user}],
        tools=[], model=model, system=system,
    )
    return _parse_decision(getattr(resp, "content", None))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watcher_decide.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/project_watcher.py tests/test_watcher_decide.py
git commit -m "feat(watcher): decision sub-agent with structured JSON output"
```

---

## Task 5: channel echo helper

A small helper to post text to a Discord channel from sync code (the daemon), reused by the tick driver and the CLI. Wraps `bemine.discord.send.send_message` via `asyncio.run`.

**Files:**
- Modify: `src/bemine/jobs/project_watcher.py` (add `echo_to_channel`)
- Test: `tests/test_watcher_echo.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watcher_echo.py
import bemine.jobs.project_watcher as pw


def test_echo_to_channel_calls_send_message(monkeypatch):
    sent = {}

    async def _fake_send(*, settings, channel_id, text, files, reply_to=None):
        sent["channel_id"] = channel_id
        sent["text"] = text
        return {"id": "1"}

    monkeypatch.setattr(pw, "send_message", _fake_send)
    monkeypatch.setattr(pw.DiscordSettings, "load", classmethod(lambda cls: object()))
    pw.echo_to_channel("c1", "hello")
    assert sent == {"channel_id": "c1", "text": "hello"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watcher_echo.py -v`
Expected: FAIL — `AttributeError: module 'bemine.jobs.project_watcher' has no attribute 'send_message'`.

- [ ] **Step 3: Write the implementation**

Add to `src/bemine/jobs/project_watcher.py` (imports at top with the others):

```python
import asyncio

from bemine.discord.config import Settings as DiscordSettings
from bemine.discord.send import send_message
```

and the helper:

```python
def echo_to_channel(channel_id: str, text: str) -> None:
    """Best-effort post to a Discord channel from sync (daemon) code."""
    if not channel_id or not text:
        return
    ds = DiscordSettings.load()

    async def _go():
        await send_message(settings=ds, channel_id=channel_id, text=text, files=[])

    try:
        asyncio.run(_go())
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watcher_echo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/project_watcher.py tests/test_watcher_echo.py
git commit -m "feat(watcher): channel echo helper"
```

---

## Task 6: tick driver

The testable core: run one watcher tick. All side-effecting collaborators (status, tmux, client, echo) are passed in via a `WatcherDeps` bundle so tests inject fakes; production builds real ones.

**Files:**
- Modify: `src/bemine/jobs/project_watcher.py` (add `WatcherDeps`, `default_deps`, `tick_watcher`)
- Test: `tests/test_watcher_tick.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watcher_tick.py
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import WatcherDeps, tick_watcher, Decision


def _setup(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="rai-speech-toolkit", channel_id="c1",
                              brief="get training running", interval_secs=300)
    return settings, db


def _status(asks_user, cwd="/Users/cdjk/github/llm/rai-speech-toolkit"):
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s", cwd=cwd,
        source_path="", last_interaction_at=1.0, age_seconds=10,
        last_role="assistant", asks_user=asks_user, ask_summary="proceed?",
        latest_summary="",
    )


class _Tmux:
    def __init__(self):
        self.sent_text = []
        self.sent_keys = []
    def list_panes(self, device):
        return [{"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                 "command": "claude"}]
    def capture(self, device, pane):
        return "Proceed? (y/n)"
    def send_text(self, device, pane, text):
        self.sent_text.append((device, pane, text))
    def send_keys(self, device, pane, keys):
        self.sent_keys.append((device, pane, keys))


def _deps(statuses, decision, tmux, echoes):
    return WatcherDeps(
        get_statuses=lambda settings: statuses,
        tmux=tmux,
        decide=lambda **kw: decision,
        echo=lambda channel_id, text: echoes.append((channel_id, text)),
        transcript_tail=lambda status: "",
    )


def test_tick_sends_text_when_session_waiting(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)], Decision(kind="send_text", text="y"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert tmux.sent_text == [("mbp", "%2", "y")]
    assert db.get_project_watcher("rai-speech-toolkit")["last_tick_at"] == 100.0
    assert any("sent" in t.lower() for _, t in echoes)


def test_tick_does_nothing_when_session_working(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=False)], Decision(kind="send_text", text="y"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert tmux.sent_text == []


def test_tick_escalates_and_pauses(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)],
                 Decision(kind="escalate", question="Which account?"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    row = db.get_project_watcher("rai-speech-toolkit")
    assert row["awaiting_reply"] == 1
    assert any("Which account?" in t for _, t in echoes)


def test_tick_done_stops_watcher(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)],
                 Decision(kind="done", rationale="training is live"), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert db.get_project_watcher("rai-speech-toolkit")["status"] == "done"


def test_tick_pauses_after_missing_session(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([], Decision(kind="wait"), tmux, echoes)  # no live session
    for _ in range(3):
        w = db.get_project_watcher("rai-speech-toolkit")
        tick_watcher(settings, db, w, deps, now=100.0)
    assert db.get_project_watcher("rai-speech-toolkit")["status"] == "paused"


def test_tick_send_keys_for_menu(tmp_path):
    settings, db = _setup(tmp_path)
    tmux, echoes = _Tmux(), []
    deps = _deps([_status(asks_user=True)],
                 Decision(kind="send_keys", keys=["Down", "Enter"]), tmux, echoes)
    tick_watcher(settings, db, db.get_project_watcher("rai-speech-toolkit"), deps, now=100.0)
    assert tmux.sent_keys == [("mbp", "%2", ["Down", "Enter"])]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watcher_tick.py -v`
Expected: FAIL — `ImportError: cannot import name 'WatcherDeps'`.

- [ ] **Step 3: Write the implementation**

Add to `src/bemine/jobs/project_watcher.py`:

```python
from pathlib import Path
from typing import Callable

from bemine.config import BemineSettings
from bemine.db import BemineDB

_MAX_EMPTY_TICKS = 3


@dataclass
class WatcherDeps:
    get_statuses: Callable           # (settings) -> list[SessionStatus]
    tmux: object                     # RemoteTmux-like
    decide: Callable                 # (**kwargs) -> Decision
    echo: Callable                   # (channel_id, text) -> None
    transcript_tail: Callable        # (SessionStatus) -> str


def default_deps(settings: BemineSettings) -> WatcherDeps:
    from bemine.index.status import collect_fleet_status, session_transcript_tail
    from bemine.jobs.remote_tmux import RemoteTmux

    def _statuses(s):
        statuses, _errors = collect_fleet_status(s)
        return statuses

    def _tail(status):
        if not status.source_path:
            return ""
        try:
            return session_transcript_tail(Path(status.source_path), status.runtime)
        except Exception:
            return ""

    return WatcherDeps(
        get_statuses=_statuses,
        tmux=RemoteTmux(gateway_device=settings.device_id),
        decide=decide,
        echo=echo_to_channel,
        transcript_tail=_tail,
    )


def tick_watcher(settings: BemineSettings, db: BemineDB, watcher: dict,
                 deps: WatcherDeps, *, now: float) -> None:
    project_id = watcher["project_id"]
    channel_id = watcher["channel_id"]
    project_name = project_id.split(":")[-1]  # "channel:<name>" or a plain name
    db.update_project_watcher(project_id, last_tick_at=now)

    statuses = deps.get_statuses(settings)
    session = resolve_project_session(statuses, project_name)
    if session is None:
        empty = int(watcher.get("empty_ticks", 0)) + 1
        db.update_project_watcher(project_id, empty_ticks=empty)
        if empty >= _MAX_EMPTY_TICKS:
            db.update_project_watcher(project_id, status="paused")
            deps.echo(channel_id,
                      f"⏸ Watcher for `{project_name}` paused — no live session found.")
        return
    db.update_project_watcher(project_id, empty_ticks=0)

    if not session.asks_user:
        return  # actively working; nothing to do

    pane = resolve_pane(deps.tmux, session.device_id, session.cwd)
    if pane is None:
        db.update_project_watcher(project_id, awaiting_reply=1)
        deps.echo(channel_id,
                  f"⚠ Watcher for `{project_name}`: session is waiting but I can't find "
                  f"its tmux pane on `{session.device_id}`. Resolve it and I'll resume.")
        return

    decision = deps.decide(
        client=_decision_client(settings, session.runtime),
        model=_decision_model(session.runtime),
        brief=watcher["brief"],
        pane_capture=deps.tmux.capture(session.device_id, pane),
        transcript_tail=deps.transcript_tail(session),
        ask_summary=session.ask_summary,
        channel_context="",
    )

    if decision.kind == "send_text":
        deps.tmux.send_text(session.device_id, pane, decision.text)
        deps.echo(channel_id, f"▶ `{project_name}` sent: {decision.text}")
    elif decision.kind == "send_keys":
        deps.tmux.send_keys(session.device_id, pane, decision.keys)
        deps.echo(channel_id, f"▶ `{project_name}` sent keys: {' '.join(decision.keys)}")
    elif decision.kind == "escalate":
        db.update_project_watcher(project_id, awaiting_reply=1)
        deps.echo(channel_id, f"🙋 `{project_name}` needs you: {decision.question}")
    elif decision.kind == "done":
        db.update_project_watcher(project_id, status="done")
        deps.echo(channel_id, f"✅ `{project_name}` watcher done: {decision.rationale}")
    # "wait" -> no action


def _decision_client(settings: BemineSettings, runtime: str):
    from bemine.agent.clients import build_client_for_runtime
    return build_client_for_runtime(settings, runtime or "kimi")


def _decision_model(runtime: str) -> str:
    from bemine.config import config_string
    from bemine.discord.dispatch import _DEFAULT_MODELS
    rt = (runtime or "kimi").lower()
    return config_string(rt, "model", "") or _DEFAULT_MODELS.get(rt, "kimi-k2.6")
```

Note: the test injects `deps.decide` as `lambda **kw: decision`, so `_decision_client` /
`_decision_model` are only exercised in production; the test never builds a real client.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watcher_tick.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/project_watcher.py tests/test_watcher_tick.py
git commit -m "feat(watcher): tick driver (send/escalate/done/pause)"
```

---

## Task 7: reply relay + tick scheduler

`relay_user_reply` feeds a human's channel reply back into the session and resumes the watcher. `tick_all_watchers` is the daemon entry point that fires due watchers.

**Files:**
- Modify: `src/bemine/jobs/project_watcher.py`
- Test: `tests/test_watcher_relay.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watcher_relay.py
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.index.status import SessionStatus
from bemine.jobs.project_watcher import WatcherDeps, relay_user_reply, tick_all_watchers


def _settings_db(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    return settings, BemineDB(settings.db_path)


def _status():
    return SessionStatus(
        device_id="mbp", runtime="claude", session_id="s",
        cwd="/Users/cdjk/github/llm/rai-speech-toolkit", source_path="",
        last_interaction_at=1.0, age_seconds=10, last_role="assistant",
        asks_user=True, ask_summary="", latest_summary="",
    )


class _Tmux:
    def __init__(self):
        self.sent_text = []
    def list_panes(self, device):
        return [{"pane_id": "%2", "path": "/Users/cdjk/github/llm/rai-speech-toolkit",
                 "command": "claude"}]
    def capture(self, device, pane):
        return ""
    def send_text(self, device, pane, text):
        self.sent_text.append(text)
    def send_keys(self, device, pane, keys):
        pass


def _deps(tmux):
    return WatcherDeps(get_statuses=lambda s: [_status()], tmux=tmux,
                       decide=lambda **kw: None, echo=lambda c, t: None,
                       transcript_tail=lambda s: "")


def test_relay_user_reply_sends_text_and_resumes(tmp_path):
    settings, db = _settings_db(tmp_path)
    db.upsert_project_watcher(project_id="rai-speech-toolkit", channel_id="c1",
                              brief="b", interval_secs=300)
    db.update_project_watcher("rai-speech-toolkit", awaiting_reply=1)
    tmux = _Tmux()
    ok = relay_user_reply(settings, db, db.get_project_watcher_by_channel("c1"),
                          "use the staging account", _deps(tmux))
    assert ok is True
    assert tmux.sent_text == ["use the staging account"]
    row = db.get_project_watcher("rai-speech-toolkit")
    assert row["awaiting_reply"] == 0
    assert row["last_tick_at"] == 0  # so it re-ticks promptly


def test_tick_all_fires_only_due_active_watchers(tmp_path):
    settings, db = _settings_db(tmp_path)
    db.upsert_project_watcher(project_id="due", channel_id="c1", brief="b", interval_secs=300)
    db.upsert_project_watcher(project_id="fresh", channel_id="c2", brief="b", interval_secs=300)
    db.update_project_watcher("fresh", last_tick_at=1000.0)
    db.update_project_watcher("due", last_tick_at=0.0)
    db.update_project_watcher("due", awaiting_reply=0)
    fired = []
    deps = _deps(_Tmux())
    tick_all_watchers(settings, db, deps=deps, now=1100.0,
                      tick=lambda s, d, w, dp, now: fired.append(w["project_id"]))
    assert fired == ["due"]  # 'fresh' not due (1000 + 300 > 1100)


def test_tick_all_skips_awaiting_reply(tmp_path):
    settings, db = _settings_db(tmp_path)
    db.upsert_project_watcher(project_id="p", channel_id="c", brief="b", interval_secs=300)
    db.update_project_watcher("p", last_tick_at=0.0, awaiting_reply=1)
    fired = []
    tick_all_watchers(settings, db, deps=_deps(_Tmux()), now=1100.0,
                      tick=lambda s, d, w, dp, now: fired.append(w["project_id"]))
    assert fired == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watcher_relay.py -v`
Expected: FAIL — `ImportError: cannot import name 'relay_user_reply'`.

- [ ] **Step 3: Write the implementation**

Add to `src/bemine/jobs/project_watcher.py`:

```python
def relay_user_reply(settings: BemineSettings, db: BemineDB, watcher: dict,
                     text: str, deps: WatcherDeps) -> bool:
    """Feed a human's channel reply into the watched session, then resume ticking.
    Returns True if the reply was delivered into a pane."""
    project_id = watcher["project_id"]
    project_name = project_id.split(":")[-1]
    session = resolve_project_session(deps.get_statuses(settings), project_name)
    if session is None:
        return False
    pane = resolve_pane(deps.tmux, session.device_id, session.cwd)
    if pane is None:
        return False
    deps.tmux.send_text(session.device_id, pane, text)
    db.update_project_watcher(project_id, awaiting_reply=0, status="active", last_tick_at=0)
    return True


def tick_all_watchers(settings: BemineSettings, db: BemineDB, *,
                      deps: WatcherDeps | None = None, now: float | None = None,
                      tick: Callable | None = None) -> int:
    """Fire every active, non-awaiting watcher whose interval has elapsed. Returns the
    number fired. `deps`/`tick` are injectable for tests."""
    import time as _time
    now = _time.time() if now is None else now
    deps = deps or default_deps(settings)
    tick = tick or tick_watcher
    fired = 0
    for watcher in db.list_active_project_watchers():
        if int(watcher.get("awaiting_reply", 0)):
            continue
        if now - float(watcher.get("last_tick_at", 0)) < int(watcher["interval_secs"]):
            continue
        try:
            tick(settings, db, watcher, deps, now=now)
            fired += 1
        except Exception:
            continue
    return fired
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watcher_relay.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/project_watcher.py tests/test_watcher_relay.py
git commit -m "feat(watcher): reply relay + due-watcher scheduler"
```

---

## Task 8: wire into the daemon

**Files:**
- Modify: `src/bemine/jobs/core.py` (`BemineDaemon.run_forever`, `run_once`)
- Test: `tests/test_jobs_daemon.py` (add a case)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_jobs_daemon.py`:

```python
def test_run_once_ticks_watchers(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.project_watcher as pw

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    called = {"n": 0}
    monkeypatch.setattr(pw, "tick_all_watchers",
                        lambda s, db: called.__setitem__("n", called["n"] + 1))
    BemineDaemon(settings).run_once()
    assert called["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jobs_daemon.py::test_run_once_ticks_watchers -v`
Expected: FAIL — watchers are not ticked by `run_once`.

- [ ] **Step 3: Modify the daemon**

In `src/bemine/jobs/core.py`, change `BemineDaemon`:

```python
    def run_once(self) -> list[JobRunResult]:
        results = run_due_jobs(self.settings)
        self._tick_watchers()
        return results

    def run_forever(self) -> None:
        db = BemineDB(self.settings.db_path)
        seed_default_jobs(db)
        while True:
            run_due_jobs(self.settings)
            self._tick_watchers()
            time.sleep(self.poll_seconds)

    def _tick_watchers(self) -> None:
        from .project_watcher import tick_all_watchers
        try:
            db = BemineDB(self.settings.db_path)
            tick_all_watchers(self.settings, db)
        except Exception:
            pass
```

Note the test monkeypatches `tick_all_watchers` to a 2-arg `(s, db)` callable, matching this call site.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jobs_daemon.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/jobs/core.py tests/test_jobs_daemon.py
git commit -m "feat(watcher): tick watchers each daemon poll"
```

---

## Task 9: `watch_control` channel tool

Lets the bound channel agent start/stop/report a watcher from plain language. The agent maps "every 10m" → `interval_secs=600` and passes the verbatim brief.

**Files:**
- Modify: `src/bemine/context/tool_registry.py` (register tool + handler)
- Test: `tests/test_watch_control_tool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watch_control_tool.py
from pathlib import Path
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.context.tool_registry import BemineToolRegistry, ToolContext


def _ctx(tmp_path, project_id="channel:rai-speech-toolkit"):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="sess1", channel_id="c1", project_id=project_id, runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    return settings, db, ToolContext(settings=settings, db=db, session_id="sess1",
                                     project_id=project_id, cwd=Path("/tmp"))


def test_watch_control_start_creates_watcher(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    out = reg.execute("watch_control",
                      {"action": "start", "brief": "get training running",
                       "interval_secs": 600}, ctx)
    assert out["status"] == "active"
    row = db.get_project_watcher("channel:rai-speech-toolkit")
    assert row["brief"] == "get training running"
    assert row["interval_secs"] == 600
    assert row["channel_id"] == "c1"


def test_watch_control_stop_and_status(tmp_path):
    settings, db, ctx = _ctx(tmp_path)
    reg = BemineToolRegistry()
    reg.execute("watch_control", {"action": "start", "brief": "b"}, ctx)
    status = reg.execute("watch_control", {"action": "status"}, ctx)
    assert status["status"] == "active"
    stopped = reg.execute("watch_control", {"action": "stop"}, ctx)
    assert stopped["status"] == "stopped"
    assert db.get_project_watcher("channel:rai-speech-toolkit")["status"] == "stopped"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watch_control_tool.py -v`
Expected: FAIL — `unknown tool: watch_control`.

- [ ] **Step 3: Register the tool + handler**

In `src/bemine/context/tool_registry.py`, add this `BemineTool(...)` to the list returned by `default_tools()` (after the `tmux_capture` tool, before `file_read`):

```python
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
```

Add the handler (near the other handlers, e.g. after `_tmux_capture`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watch_control_tool.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/context/tool_registry.py tests/test_watch_control_tool.py
git commit -m "feat(watcher): watch_control channel tool (start/stop/status)"
```

---

## Task 10: gateway consumes replies for an awaiting watcher

When a watcher in a channel is `awaiting_reply`, the next user message there is the answer: relay it into the session and resume, instead of running the normal channel agent.

**Files:**
- Modify: `src/bemine/discord/gateway.py` (`_handle_allowed_message`, before the dispatch block at line ~570)
- Test: `tests/test_gateway_watcher_reply.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gateway_watcher_reply.py
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.discord.gateway import maybe_consume_watcher_reply


def test_consume_when_awaiting(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)
    db.update_project_watcher("channel:p", awaiting_reply=1)

    import bemine.jobs.project_watcher as pw
    monkeypatch.setattr(pw, "relay_user_reply", lambda s, d, w, text, deps: True)

    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="use staging")
    assert handled is True


def test_not_consumed_when_not_awaiting(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)  # awaiting_reply defaults to 0
    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="hi")
    assert handled is False


def test_not_consumed_when_no_watcher(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    handled = maybe_consume_watcher_reply(settings, db, channel_id="cX", text="hi")
    assert handled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gateway_watcher_reply.py -v`
Expected: FAIL — `cannot import name 'maybe_consume_watcher_reply'`.

- [ ] **Step 3: Add the helper + call site**

Add to `src/bemine/discord/gateway.py` (module-level function, near `run_bound_agent`):

```python
def maybe_consume_watcher_reply(settings, db, *, channel_id: str, text: str) -> bool:
    """If a watcher in this channel is awaiting a human reply, relay `text` into its
    session and resume the watcher. Returns True if the message was consumed."""
    if not text:
        return False
    watcher = db.get_project_watcher_by_channel(channel_id)
    if not watcher or not int(watcher.get("awaiting_reply", 0)):
        return False
    from bemine.jobs.project_watcher import relay_user_reply, default_deps
    relay_user_reply(settings, db, watcher, text, default_deps(settings))
    return True
```

Then in `_handle_allowed_message`, inside the `if normalized or saved_attachments:` block (around line 570), BEFORE the dispatch-task setup, add:

```python
            from bemine.db import BemineDB as _BemineDB
            _db = _BemineDB(self.settings.db_path)
            if await asyncio.to_thread(
                maybe_consume_watcher_reply, BemineSettings.load(), _db,
                channel_id=channel_key, text=normalized,
            ):
                await responder.send("↪ Relayed your reply into the watched session.")
                return
```

(`channel_key = str(message.channel.id)` is already defined at the top of that block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_gateway_watcher_reply.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bemine/discord/gateway.py tests/test_gateway_watcher_reply.py
git commit -m "feat(watcher): gateway relays channel replies to an awaiting watcher"
```

---

## Task 11: `bemine watch` CLI

**Files:**
- Modify: `src/bemine/cli.py` (`build_parser` add subparser; `main` add dispatch block)
- Test: `tests/test_watch_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watch_cli.py
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.cli import main


def _prime(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c1", project_id="channel:demo", runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    return settings, db


def test_cli_watch_start_status_stop(tmp_path, capsys):
    settings, db = _prime(tmp_path)
    base = ["--state-dir", str(settings.state_dir)]
    main(base + ["watch", "start", "--project", "channel:demo",
                 "--brief", "drive it", "--interval", "120"])
    row = db.get_project_watcher("channel:demo")
    assert row["status"] == "active"
    assert row["interval_secs"] == 120
    assert row["channel_id"] == "c1"

    main(base + ["watch", "list"])
    assert "channel:demo" in capsys.readouterr().out

    main(base + ["watch", "stop", "--project", "channel:demo"])
    assert db.get_project_watcher("channel:demo")["status"] == "stopped"
```

Note: the global state-dir flag is `--state-dir` (confirmed in `build_parser`, `src/bemine/cli.py:238`), parsed before the subcommand.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watch_cli.py -v`
Expected: FAIL — argparse error: invalid choice: 'watch'.

- [ ] **Step 3: Add the subparser**

In `src/bemine/cli.py` `build_parser()`, after an existing subparser block (e.g. the `jobs` one), add:

```python
    watch = sub.add_parser("watch", help="Manage per-project watchers.")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    w_start = watch_sub.add_parser("start", help="Start/replace a watcher for a project.")
    w_start.add_argument("--project", required=True, help="project id (e.g. channel:<name>)")
    w_start.add_argument("--brief", required=True, help="context brief / goal")
    w_start.add_argument("--interval", type=int, default=300, help="poll interval seconds")
    w_stop = watch_sub.add_parser("stop", help="Stop a watcher.")
    w_stop.add_argument("--project", required=True)
    w_status = watch_sub.add_parser("status", help="Show a watcher.")
    w_status.add_argument("--project", required=True)
    watch_sub.add_parser("list", help="List all watchers.")
```

- [ ] **Step 4: Add the dispatch block**

In `src/bemine/cli.py` `main()`, add an `elif` alongside the other commands (e.g. after the `jobs` block):

```python
    elif args.command == "watch":
        from bemine.db import BemineDB
        settings = _settings_from_args(args)
        db = BemineDB(settings.db_path)
        if args.watch_command == "start":
            channel_id = db.get_channel_id_for_project(args.project) or ""
            db.upsert_project_watcher(
                project_id=args.project, channel_id=channel_id,
                brief=args.brief, interval_secs=args.interval,
            )
            print(f"watcher active for {args.project} (every {args.interval}s, "
                  f"channel={channel_id or 'unknown'})")
        elif args.watch_command == "stop":
            db.update_project_watcher(args.project, status="stopped")
            print(f"watcher stopped for {args.project}")
        elif args.watch_command == "status":
            row = db.get_project_watcher(args.project)
            print(json.dumps(row, indent=2) if row else "no watcher")
        elif args.watch_command == "list":
            for row in db.list_active_project_watchers():
                print(f"{row['project_id']}\t{row['status']}\t"
                      f"every {row['interval_secs']}s\tchannel={row['channel_id']}")
```

Confirm `json` is imported at the top of `src/bemine/cli.py` (it is used elsewhere); if not, add `import json`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_watch_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bemine/cli.py tests/test_watch_cli.py
git commit -m "feat(watcher): bemine watch CLI (start/stop/status/list)"
```

---

## Task 12: docs + full-suite gate

**Files:**
- Modify: `SKILL.md` (Important Surfaces + a Watcher note)
- Modify: `README.md` (a short Watcher section)

- [ ] **Step 1: Update `SKILL.md`**

Under "Important Surfaces", add:

```markdown
- Project watcher: `src/bemine/jobs/project_watcher.py` drives a tmux-hosted session
  toward a per-project brief; `src/bemine/jobs/remote_tmux.py` runs tmux locally or over
  SSH. Control via the `watch_control` channel tool or `bemine watch`.
```

- [ ] **Step 2: Add a `README.md` section**

Add near the jobs/daemon documentation:

```markdown
### Watcher mode

Enable a watcher from a project's Discord channel: "watch this, goal: <your full
context>, check every 10m". When the project's session is waiting on input, Bemine
drives it toward your brief (sending keystrokes over SSH for remote devices) and only
messages you when blocked — your reply in the channel is relayed straight back in.
Every action is echoed to the channel. Stop with "stop watching"; inspect with
`bemine watch list`.
```

- [ ] **Step 3: Run the full suite + lint**

Run:
```bash
uv run pytest -q
uv run ruff check src/bemine/jobs/project_watcher.py src/bemine/jobs/remote_tmux.py \
  src/bemine/db.py src/bemine/context/tool_registry.py src/bemine/discord/gateway.py \
  src/bemine/cli.py src/bemine/jobs/core.py
```
Expected: all tests PASS; ruff clean.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md README.md
git commit -m "docs(watcher): document watcher mode (SKILL + README)"
```

---

## Manual verification (after implementation)

1. Restart services: `systemctl --user restart bemine-daemon mayor-discord-gateway`.
2. In a project channel whose session runs in tmux on a reachable device, send:
   "watch this. goal: <lengthy brief>. check every 5m".
3. Confirm: a "watcher active" reply; then, when that session next waits on input, a
   "▶ sent: …" echo appears, or a "🙋 needs you: …" escalation.
4. Reply to an escalation in the channel; confirm "↪ Relayed your reply…" and that the
   session advances.
5. `bemine watch list` shows the active watcher; "stop watching" clears it.

---

## Self-review notes (author)

- **Spec coverage:** topology/SSH (Task 2), tmux-on-each-device drive (Tasks 2–3),
  autonomous+escalate decision (Tasks 4, 6), plain-language control (Task 9) + CLI
  (Task 11), context brief stored verbatim + injected (Tasks 1, 4, 6, 9), escalation↔reply
  (Tasks 7, 10), interactive menus via send_keys (Tasks 2, 4, 6), visibility/echo +
  audit (Tasks 5, 6 — note: `echo` is the channel visibility; an `audit_events` row can be
  added inside `tick_watcher` via `db.record_audit_event` if desired but is optional and
  not separately tested), safety bounds: missing-session pause (Task 6), interval gating
  (Task 7). Loop-protection on unchanged-pane (spec §safety) is intentionally deferred —
  see below.
- **Deferred (call out to user before/after build):** unchanged-pane loop-protection and
  `max_ticks`/`max_wall_clock` bounds from spec §7 are not in these tasks to keep v1
  focused; the missing-session pause + escalation already prevent the worst runaway. Add
  as a fast follow if desired.
- **Type consistency:** `WatcherDeps` fields (`get_statuses`, `tmux`, `decide`, `echo`,
  `transcript_tail`) are used identically in Tasks 6–7. `Decision` fields
  (`kind/text/keys/question/rationale`) match across Tasks 4 and 6. DB method names match
  across all tasks.
