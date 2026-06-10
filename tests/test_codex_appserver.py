"""Unit tests for bemine.agent.codex_appserver.

Tests drive CodexAppServerSession directly with fake transports — no real
codex subprocess is spawned.
"""
from __future__ import annotations

import json
import queue
from io import StringIO
from typing import Iterator

import pytest

from bemine.agent.codex_appserver import (
    CodexAppServerError,
    CodexAppServerSession,
    _queue_iter,
    _SENTINEL,
    run_codex_turn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session(server_messages: list[dict], **kwargs) -> tuple[list[dict], CodexAppServerSession]:
    """Build a (sent_log, session) pair driven by a scripted server message list."""
    sent: list[dict] = []

    def _send(msg: dict) -> None:
        sent.append(msg)

    msgs = iter(server_messages)

    def _recv() -> Iterator[dict]:
        for m in msgs:
            yield m

    session = CodexAppServerSession(
        send_fn=_send,
        recv_iter=_recv(),
        **kwargs,
    )
    return sent, session


# ---------------------------------------------------------------------------
# Happy-path test
# ---------------------------------------------------------------------------

def test_happy_path_returns_item_completed_text():
    server_msgs = [
        # initialize response
        {"id": 1, "result": {"serverInfo": {"name": "codex"}}},
        # thread/start response
        {"id": 2, "result": {"thread": {"id": "tid-abc"}}},
        # turn/start response (optional ack)
        {"id": 3, "result": {}},
        # agent deltas
        {"method": "item/agentMessage/delta", "params": {"delta": "po"}},
        {"method": "item/agentMessage/delta", "params": {"delta": "ng"}},
        # item completed (authoritative)
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "pong"}}},
        # turn completed
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(
        server_msgs,
        prompt="Reply with exactly one word: pong",
        cwd="/tmp",
        timeout=30.0,
    )
    result = session.run()
    assert result == "pong"


def test_happy_path_fallback_to_deltas_when_no_item_completed():
    """If no item/completed arrives, fall back to joined deltas."""
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "tid-xyz"}}},
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "he"}},
        {"method": "item/agentMessage/delta", "params": {"delta": "llo"}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="hi", cwd="/tmp", timeout=30.0)
    result = session.run()
    assert result == "hello"


def test_sends_correct_initialize_message():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="x", cwd="/repo", timeout=30.0)
    session.run()
    assert sent[0] == {
        "id": 1,
        "method": "initialize",
        "params": {"clientInfo": {"name": "bemine", "version": "0.1.0"}},
    }


def test_sends_thread_start_with_sandbox_and_cwd():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(
        server_msgs,
        prompt="x",
        cwd="/my/project",
        sandbox="danger-full-access",
        timeout=30.0,
    )
    session.run()
    thread_start = sent[1]
    assert thread_start["method"] == "thread/start"
    params = thread_start["params"]
    assert params["cwd"] == "/my/project"
    assert params["sandbox"] == "danger-full-access"
    assert params["approvalPolicy"] == "never"


def test_model_included_when_provided():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(
        server_msgs,
        prompt="x",
        cwd="/tmp",
        model="o4-mini",
        timeout=30.0,
    )
    session.run()
    params = sent[1]["params"]
    assert params.get("model") == "o4-mini"


def test_model_omitted_when_none():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="x", cwd="/tmp", model=None, timeout=30.0)
    session.run()
    params = sent[1]["params"]
    assert "model" not in params


def test_developer_instructions_included():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(
        server_msgs,
        prompt="x",
        cwd="/tmp",
        developer_instructions="You are a helper.",
        timeout=30.0,
    )
    session.run()
    params = sent[1]["params"]
    assert params.get("developerInstructions") == "You are a helper."


def test_developer_instructions_omitted_when_empty():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(
        server_msgs, prompt="x", cwd="/tmp", developer_instructions="", timeout=30.0
    )
    session.run()
    params = sent[1]["params"]
    assert "developerInstructions" not in params


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_error_message_raises():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"error": {"code": -32000, "message": "something went wrong"}},
    ]
    _, session = _session(server_msgs, prompt="x", cwd="/tmp", timeout=30.0)
    with pytest.raises(CodexAppServerError, match="something went wrong"):
        session.run()


def test_initialize_error_response_raises():
    server_msgs = [
        {"id": 1, "error": {"code": -1, "message": "init failed"}},
    ]
    _, session = _session(server_msgs, prompt="x", cwd="/tmp", timeout=30.0)
    with pytest.raises(CodexAppServerError, match="init failed"):
        session.run()


def test_missing_thread_id_raises():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {}},  # no thread.id
    ]
    _, session = _session(server_msgs, prompt="x", cwd="/tmp", timeout=30.0)
    with pytest.raises(CodexAppServerError, match="result.thread.id"):
        session.run()


def test_stream_ends_before_turn_completed_raises():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        # stream ends here without turn/completed
    ]
    _, session = _session(server_msgs, prompt="x", cwd="/tmp", timeout=30.0)
    with pytest.raises(CodexAppServerError, match="stream ended"):
        session.run()


def test_approval_request_denied():
    """Approval requests from server get a deny response."""
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        # Server sends an approval request
        {"id": 99, "method": "applyPatchApproval", "params": {"patch": "..."}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "done"}}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="x", cwd="/tmp", timeout=30.0)
    result = session.run()
    assert result == "done"
    # Should have sent a denial
    denial = next((m for m in sent if m.get("id") == 99), None)
    assert denial is not None
    assert denial["result"]["decision"] == "deny"


# ---------------------------------------------------------------------------
# queue_iter timeout test
# ---------------------------------------------------------------------------

def test_queue_iter_timeout_raises():
    q: queue.Queue = queue.Queue()
    # Nothing in queue — iterator should raise CodexAppServerError quickly
    it = _queue_iter(q, timeout=0.05)
    with pytest.raises(CodexAppServerError, match="timed out"):
        next(it)


def test_queue_iter_sentinel_stops():
    q: queue.Queue = queue.Queue()
    q.put({"id": 1, "result": {}})
    q.put(_SENTINEL)
    results = list(_queue_iter(q, timeout=1.0))
    assert results == [{"id": 1, "result": {}}]


# ---------------------------------------------------------------------------
# run_codex_turn with fake subprocess
# ---------------------------------------------------------------------------

class _FakeStream:
    """Emulates a text-mode stdout stream from a list of JSON dicts."""

    def __init__(self, messages: list[dict]) -> None:
        lines = [json.dumps(m) + "\n" for m in messages]
        self._io = StringIO("".join(lines))

    def __iter__(self):
        return self

    def __next__(self):
        line = self._io.readline()
        if not line:
            raise StopIteration
        return line


class _FakeProc:
    """Minimal Popen-like object for tests."""

    def __init__(self, server_messages: list[dict]) -> None:
        self._stdin_lines: list[str] = []
        self.stdout = _FakeStream(server_messages)
        self.stderr = _FakeStream([])
        self.returncode = 0

    @property
    def stdin(self):
        return self

    def write(self, data: str) -> None:
        self._stdin_lines.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def terminate(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        return 0


def _make_fake_spawn(server_messages: list[dict]):
    def _spawn(codex_bin: str) -> _FakeProc:
        return _FakeProc(server_messages)
    return _spawn


def test_run_codex_turn_with_fake_subprocess():
    from bemine.agent.codex_appserver import _SHARED
    _SHARED.kill()
    server_msgs = [
        {"id": 1, "result": {"serverInfo": {"name": "codex"}}},
        {"id": 2, "result": {"thread": {"id": "thread-1"}}},
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "po"}},
        {"method": "item/agentMessage/delta", "params": {"delta": "ng"}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "pong"}}},
        {"method": "turn/completed", "params": {}},
    ]
    result, thread_id = run_codex_turn(
        "Reply with exactly one word: pong",
        cwd="/tmp",
        sandbox="read-only",
        _spawn=_make_fake_spawn(server_msgs),
        persistent=False,
    )
    assert result == "pong"
    assert thread_id == "thread-1"
    _SHARED.kill()


def test_run_codex_turn_error_raises():
    from bemine.agent.codex_appserver import _SHARED
    _SHARED.kill()
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t1"}}},
        {"id": 3, "result": {}},
        {"error": {"code": -32001, "message": "quota exceeded"}},
    ]
    with pytest.raises(CodexAppServerError):
        run_codex_turn(
            "hello",
            cwd="/tmp",
            _spawn=_make_fake_spawn(server_msgs),
            persistent=False,
        )
    _SHARED.kill()


def test_mcp_startup_failure_notification_is_nonfatal():
    """A failing optional MCP server (e.g. proton_bridge timing out) arrives as an
    mcpServer/startupStatus/updated notification with an error — codex itself continues,
    so the turn must too. Regression: this used to raise and kill every turn."""
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "tid-mcp"}}},
        {"id": 3, "result": {}},
        {"method": "mcpServer/startupStatus/updated",
         "params": {"error": "MCP client for `proton_bridge` timed out after 30 seconds."}},
        {"method": "item/agentMessage/delta", "params": {"delta": "pong"}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="ping", cwd="/tmp", timeout=30.0)
    assert session.run() == "pong"


def test_real_notification_error_still_raises():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "tid-err"}}},
        {"id": 3, "result": {}},
        {"method": "turn/error", "params": {"error": "model exploded"}},
    ]
    sent, session = _session(server_msgs, prompt="ping", cwd="/tmp", timeout=30.0)
    try:
        session.run()
        assert False, "expected CodexAppServerError"
    except CodexAppServerError as exc:
        assert "model exploded" in str(exc)


# ---------------------------------------------------------------------------
# Thread resume (Part A)
# ---------------------------------------------------------------------------

def test_resume_thread_id_sends_thread_resume_and_skips_start():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "tid-old"}}},  # thread/resume response
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "hi again"}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="again", cwd="/tmp", timeout=30.0,
                             resume_thread_id="tid-old")
    assert session.run() == "hi again"
    methods = [m.get("method") for m in sent]
    assert "thread/resume" in methods
    assert "thread/start" not in methods
    resume = next(m for m in sent if m.get("method") == "thread/resume")
    assert resume["params"]["threadId"] == "tid-old"
    assert session.thread_id == "tid-old"


def test_resume_falls_back_to_thread_start_on_error():
    # With the id counter: initialize=1, resume=2 (fails), start=3, turn/start=4
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "error": {"code": -1, "message": "unknown thread"}},  # resume fails
        {"id": 3, "result": {"thread": {"id": "tid-new"}}},             # fallback start
        {"id": 4, "result": {}},                                        # turn/start ack
        {"method": "item/agentMessage/delta", "params": {"delta": "fresh"}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="again", cwd="/tmp", timeout=30.0,
                             resume_thread_id="tid-gone")
    assert session.run() == "fresh"
    methods = [m.get("method") for m in sent]
    assert methods.count("thread/resume") == 1
    assert methods.count("thread/start") == 1
    assert session.thread_id == "tid-new"


def test_fresh_session_exposes_new_thread_id():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "tid-fresh"}}},
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "x"}},
        {"method": "turn/completed", "params": {}},
    ]
    sent, session = _session(server_msgs, prompt="p", cwd="/tmp", timeout=30.0)
    session.run()
    assert session.thread_id == "tid-fresh"


# ---------------------------------------------------------------------------
# Progress callback (Part B)
# ---------------------------------------------------------------------------

def test_progress_callback_receives_item_events():
    server_msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t"}}},
        {"id": 3, "result": {}},
        {"method": "item/started", "params": {"item": {"type": "commandExecution", "command": "pytest -q"}}},
        {"method": "item/completed", "params": {"item": {"type": "commandExecution", "command": "pytest -q", "exitCode": 0}}},
        {"method": "item/started", "params": {"item": {"type": "fileChange", "path": "src/x.py"}}},
        {"method": "item/agentMessage/delta", "params": {"delta": "done"}},
        {"method": "turn/completed", "params": {}},
    ]
    seen = []
    sent, session = _session(server_msgs, prompt="p", cwd="/tmp", timeout=30.0,
                             progress=seen.append)
    assert session.run() == "done"
    assert any("pytest -q" in s for s in seen)
    assert any("x.py" in s for s in seen)


# ---------------------------------------------------------------------------
# Persistent shared server (Part C)
# ---------------------------------------------------------------------------

class _StdinRecorder:
    """Write-recorder for FakeProc.stdin — supports getvalue() after close."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._closed = False

    def write(self, data: str) -> None:
        if not self._closed:
            self._parts.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True

    def getvalue(self) -> str:
        return "".join(self._parts)


class _FakeProcPersistent:
    """Popen-like for persistent-server tests.

    stdout is a _FakeStream over a list of messages; stdin is a recorder;
    poll() returns None unless terminate() has been called.
    """

    def __init__(self, lines: list[dict]) -> None:
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream([])
        self.stdin = _StdinRecorder()
        self._dead = False

    def poll(self) -> "int | None":
        return 0 if self._dead else None

    def terminate(self) -> None:
        self._dead = True

    def wait(self, timeout=None) -> int:
        return 0


def test_persistent_server_reused_and_initialized_once():
    from bemine.agent.codex_appserver import _SHARED, run_codex_turn
    _SHARED.kill()
    turn1 = [
        {"id": 1, "result": {}},                                  # initialize
        {"id": 2, "result": {"thread": {"id": "t1"}}},            # thread/start
        {"id": 3, "result": {}},                                  # turn/start ack
        {"method": "item/agentMessage/delta", "params": {"delta": "one"}},
        {"method": "turn/completed", "params": {}},
        # turn 2 messages on the SAME stream (no initialize response — none expected)
        {"id": 4, "result": {"thread": {"id": "t1"}}},            # thread/resume
        {"id": 5, "result": {}},                                  # turn/start ack
        {"method": "item/agentMessage/delta", "params": {"delta": "two"}},
        {"method": "turn/completed", "params": {}},
    ]
    spawned = []

    def fake_spawn(codex_bin):
        proc = _FakeProcPersistent(turn1)
        spawned.append(proc)
        return proc

    text1, tid1 = run_codex_turn("a", cwd="/tmp", codex_bin="codex", timeout=5.0,
                                 _spawn=fake_spawn)
    text2, tid2 = run_codex_turn("b", cwd="/tmp", codex_bin="codex", timeout=5.0,
                                 _spawn=fake_spawn, resume_thread_id=tid1)
    assert (text1, text2) == ("one", "two")
    assert tid1 == tid2 == "t1"
    assert len(spawned) == 1                      # ONE process for both turns
    sent = spawned[0].stdin.getvalue()
    assert sent.count('"initialize"') == 1        # initialized once
    _SHARED.kill()


def test_persistent_server_respawns_when_dead():
    from bemine.agent.codex_appserver import _SHARED, run_codex_turn
    _SHARED.kill()
    base = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t"}}},
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "ok"}},
        {"method": "turn/completed", "params": {}},
    ]
    spawned = []

    def fake_spawn(codex_bin):
        proc = _FakeProcPersistent(list(base))
        spawned.append(proc)
        return proc

    run_codex_turn("a", cwd="/tmp", codex_bin="codex", timeout=5.0, _spawn=fake_spawn)
    _SHARED.proc.terminate()                       # simulate server death
    text, _ = run_codex_turn("b", cwd="/tmp", codex_bin="codex", timeout=5.0,
                             _spawn=fake_spawn)
    assert text == "ok"
    assert len(spawned) == 2                       # respawned
    _SHARED.kill()


def test_non_persistent_path_spawns_and_terminates():
    from bemine.agent.codex_appserver import _SHARED, run_codex_turn
    _SHARED.kill()
    msgs = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "t"}}},
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "solo"}},
        {"method": "turn/completed", "params": {}},
    ]
    procs = []

    def fake_spawn(codex_bin):
        p = _FakeProcPersistent(msgs)
        procs.append(p)
        return p

    text, _ = run_codex_turn("a", cwd="/tmp", codex_bin="codex", timeout=5.0,
                             _spawn=fake_spawn, persistent=False)
    assert text == "solo"
    assert procs[0]._dead is True                  # terminated after the turn
    assert not _SHARED.alive()
    _SHARED.kill()
