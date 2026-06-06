"""Unit tests for bemine.agent.codex_appserver.

Tests drive CodexAppServerSession directly with fake transports — no real
codex subprocess is spawned.
"""
from __future__ import annotations

import json
import queue
import threading
from io import StringIO
from typing import Iterator
from unittest.mock import MagicMock, patch

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
    server_msgs = [
        {"id": 1, "result": {"serverInfo": {"name": "codex"}}},
        {"id": 2, "result": {"thread": {"id": "thread-1"}}},
        {"id": 3, "result": {}},
        {"method": "item/agentMessage/delta", "params": {"delta": "po"}},
        {"method": "item/agentMessage/delta", "params": {"delta": "ng"}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "pong"}}},
        {"method": "turn/completed", "params": {}},
    ]
    result = run_codex_turn(
        "Reply with exactly one word: pong",
        cwd="/tmp",
        sandbox="read-only",
        _spawn=_make_fake_spawn(server_msgs),
    )
    assert result == "pong"


def test_run_codex_turn_error_raises():
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
        )
