"""Codex app-server client for bemine.

Runs one Codex turn via the app-server (ChatGPT subscription), using
newline-delimited JSON-RPC over stdio. Returns final agent text.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterator

from bemine.index.status import _redact

LOG = logging.getLogger("bemine.agent.codex_appserver")


class CodexAppServerError(RuntimeError):
    """Raised on protocol error, timeout, or non-completion."""


# ---------------------------------------------------------------------------
# JSON-RPC session (pure logic, injectable transport for tests)
# ---------------------------------------------------------------------------

class CodexAppServerSession:
    """Drives the JSON-RPC handshake with codex app-server.

    ``send_fn``: callable(dict) that serialises + sends one JSON line.
    ``recv_iter``: iterator of parsed JSON dicts from the server.
    ``timeout``: overall seconds to wait for turn/completed.
    ``resume_thread_id``: if set, attempt thread/resume before thread/start.
    ``progress``: optional callable(str) for live item event labels.
    ``skip_initialize``: if True, skip the initialize handshake (process already
        initialized in a previous turn).
    ``id_alloc``: optional callable that returns the next rpc id; if provided,
        replaces the internal counter (used by the shared-server manager so ids
        never repeat across turns on one process).
    """

    def __init__(
        self,
        send_fn: Callable[[dict], None],
        recv_iter: Iterator[dict],
        *,
        timeout: float = 180.0,
        cwd: str | Path = ".",
        developer_instructions: str = "",
        sandbox: str = "workspace-write",
        model: str | None = None,
        prompt: str,
        resume_thread_id: str | None = None,
        progress: Callable[[str], None] | None = None,
        skip_initialize: bool = False,
        id_alloc: Callable[[], int] | None = None,
    ) -> None:
        self._send = send_fn
        self._recv = recv_iter
        self._timeout = timeout
        self._cwd = str(cwd)
        self._developer_instructions = developer_instructions
        self._sandbox = sandbox
        self._model = model
        self._prompt = prompt
        self._resume_thread_id = resume_thread_id
        self._progress = progress
        self._skip_initialize = skip_initialize
        self._next_id = 0
        if id_alloc is not None:
            self._rpc_id = id_alloc  # type: ignore[method-assign]
        self.thread_id: str | None = None

    def _rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def run(self) -> str:
        """Execute the full handshake and return the final agent text."""
        # Step 1: initialize (skipped when reusing a persistent process)
        if not self._skip_initialize:
            init_id = self._rpc_id()
            self._send({"id": init_id, "method": "initialize",
                        "params": {"clientInfo": {"name": "bemine", "version": "0.1.0"}}})
            self._await_response(id=init_id)

        # Step 2: thread/resume or thread/start
        thread_id: str | None = None
        if self._resume_thread_id:
            rid = self._rpc_id()
            self._send({"id": rid, "method": "thread/resume",
                        "params": {"threadId": self._resume_thread_id}})
            try:
                resp = self._await_response(id=rid)
                thread_id = (
                    ((resp.get("result") or {}).get("thread") or {}).get("id")
                    or self._resume_thread_id
                )
            except CodexAppServerError as exc:
                LOG.warning("thread/resume failed (%s); starting a fresh thread", exc)
                thread_id = None

        if thread_id is None:
            thread_params: dict = {
                "cwd": self._cwd,
                "approvalPolicy": "never",
                "sandbox": self._sandbox,
            }
            if self._developer_instructions:
                thread_params["developerInstructions"] = self._developer_instructions
            if self._model:
                thread_params["model"] = self._model
            start_id = self._rpc_id()
            self._send({"id": start_id, "method": "thread/start", "params": thread_params})
            resp_start = self._await_response(id=start_id)
            try:
                thread_id = resp_start["result"]["thread"]["id"]
            except (KeyError, TypeError) as exc:
                raise CodexAppServerError(
                    f"thread/start response missing result.thread.id: {resp_start}"
                ) from exc

        self.thread_id = thread_id

        # Step 3: turn/start
        turn_id = self._rpc_id()
        self._send({
            "id": turn_id,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": self._prompt}],
            },
        })

        # Step 4: collect until turn/completed
        return self._collect_turn()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recv_one(self) -> dict:
        """Get next message, respecting overall timeout via StopIteration."""
        try:
            return next(self._recv)
        except StopIteration:
            raise CodexAppServerError(
                "codex app-server stream ended before turn/completed"
            )

    def _await_response(self, *, id: int) -> dict:
        """Read messages until we see a response with the given id."""
        while True:
            msg = self._recv_one()
            self._maybe_handle_approval_request(msg)
            if msg.get("id") == id and "method" not in msg:
                if "error" in msg:
                    raise CodexAppServerError(
                        f"JSON-RPC error for id={id}: {msg['error']}"
                    )
                return msg

    def _collect_turn(self) -> str:
        """Read notification messages until turn/completed; return final text."""
        deltas: list[str] = []
        final_text: str | None = None

        while True:
            msg = self._recv_one()

            # Check top-level error
            if "error" in msg and "method" not in msg and "id" not in msg:
                raise CodexAppServerError(f"codex app-server error: {msg['error']}")

            # Server→client REQUEST (has both id and method) — handle approval/deny
            if "id" in msg and "method" in msg:
                self._maybe_handle_approval_request(msg)
                continue

            # Notification (no id) — dispatch on method
            method = msg.get("method", "")

            if method == "item/agentMessage/delta":
                delta = (msg.get("params") or {}).get("delta", "")
                if delta:
                    deltas.append(delta)

            elif method in ("item/started", "item/completed"):
                item = (msg.get("params") or {}).get("item") or {}
                itype = item.get("type", "")
                if itype == "agentMessage":
                    if method == "item/completed":
                        text = item.get("text")
                        if text is not None:
                            final_text = text
                else:
                    self._emit_progress(_progress_label(method, item))

            elif method == "turn/completed":
                # Authoritative end — prefer item/completed text, else joined deltas
                if final_text is not None:
                    return final_text
                return "".join(deltas)

            elif method and (msg.get("params") or {}).get("error"):
                # Status/startup notifications (e.g. an optional MCP server failing to
                # start) are informational — codex itself continues the turn without it,
                # so we must too. Only non-status notification errors abort the turn.
                if method.startswith("mcpServer/") or "status" in method.lower():
                    LOG.warning(
                        "codex notification (non-fatal) %s: %s",
                        method, msg["params"]["error"],
                    )
                    self._emit_progress(
                        f"codex warning: {_compact_progress(method)}: "
                        f"{_compact_progress(str(msg['params']['error']))}"
                    )
                    continue
                raise CodexAppServerError(
                    f"codex notification error ({method}): {msg['params']['error']}"
                )

    def _emit_progress(self, label: str) -> None:
        if not self._progress or not label:
            return
        try:
            self._progress(label)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_handle_approval_request(self, msg: dict) -> None:
        """If msg looks like a server-initiated approval request, deny it."""
        msg_id = msg.get("id")
        method = msg.get("method", "")
        if msg_id is not None and method and (
            "approval" in method.lower() or "Approval" in method
        ):
            LOG.warning(
                "codex app-server sent approval request method=%r id=%r — denying",
                method, msg_id,
            )
            try:
                self._send({"id": msg_id, "result": {"decision": "deny"}})
            except Exception as exc:  # noqa: BLE001
                LOG.debug("could not send denial: %s", exc)


def _progress_label(method: str, item: dict) -> str:
    item_type = str(item.get("type") or "")
    if not item_type:
        return ""
    if item_type == "commandExecution":
        command = _compact_progress(str(item.get("command") or "shell command"))
        if method == "item/started":
            return f"codex shell started: {command}"
        exit_code = item.get("exitCode", item.get("exit_code"))
        if exit_code in {0, "0"}:
            return f"codex shell done: {command}"
        if exit_code is None:
            return f"codex shell completed: {command}"
        return f"codex shell exit {exit_code}: {command}"
    if item_type == "fileChange":
        path = _compact_progress(str(item.get("path") or item.get("file") or "file"))
        action = "started" if method == "item/started" else "completed"
        return f"codex file {action}: {path}"
    label = _compact_progress(
        str(item.get("name") or item.get("path") or item.get("query") or item_type)
    )
    action = "started" if method == "item/started" else "completed"
    return f"codex {item_type} {action}: {label}"


def _compact_progress(value: str, *, limit: int = 180) -> str:
    text = " ".join(_redact(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Subprocess-backed session
# ---------------------------------------------------------------------------

def _spawn_appserver(codex_bin: str) -> subprocess.Popen:
    return subprocess.Popen(
        [codex_bin, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )


def _make_queue_reader(
    stream,
    q: "queue.Queue[dict | Exception]",
    timeout: float,
) -> threading.Thread:
    """Daemon thread that reads JSON lines from *stream* and posts to *q*."""

    def _read() -> None:
        try:
            for raw_line in stream:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    LOG.debug("codex app-server non-JSON line: %r (%s)", raw_line, exc)
                    continue
                q.put(msg)
        except Exception as exc:  # noqa: BLE001
            q.put(exc)
        finally:
            q.put(_SENTINEL)

    t = threading.Thread(target=_read, daemon=True)
    return t


_SENTINEL = object()  # signals EOF from reader thread


def _queue_iter(q: "queue.Queue", timeout: float) -> Iterator[dict]:
    """Iterator over messages posted to *q*, with per-item timeout."""
    while True:
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            raise CodexAppServerError(
                f"timed out after {timeout:.0f}s waiting for codex app-server response"
            )
        if item is _SENTINEL:
            return  # EOF
        if isinstance(item, Exception):
            raise CodexAppServerError(
                f"reader thread error: {item}"
            ) from item
        yield item


# ---------------------------------------------------------------------------
# Persistent shared-server manager
# ---------------------------------------------------------------------------

class _SharedServer:
    """A persistent codex app-server process shared across turns.

    One JSON-RPC ``initialize`` per process lifetime; a threading.Lock
    serialises turns.  The rpc-id counter lives here so ids never repeat
    across turns on one process.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc = None
        self.queue: "queue.Queue | None" = None
        self.codex_bin = ""
        self.initialized = False
        self.next_id = 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None
        self.queue = None
        self.initialized = False
        self.next_id = 0

    def ensure(self, codex_bin: str, timeout: float, spawn) -> None:  # type: ignore[type-arg]
        if self.alive() and self.codex_bin == codex_bin:
            return
        self.kill()
        self.proc = spawn(codex_bin)
        self.codex_bin = codex_bin
        self.queue = queue.Queue()
        _make_queue_reader(self.proc.stdout, self.queue, timeout).start()
        threading.Thread(
            target=lambda: [None for _ in self.proc.stderr],
            daemon=True,
        ).start()  # drain stderr


_SHARED = _SharedServer()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_codex_turn(
    prompt: str,
    *,
    cwd: str | Path,
    developer_instructions: str = "",
    model: str | None = None,
    sandbox: str = "workspace-write",
    codex_bin: str = "codex",
    timeout: float = 180.0,
    resume_thread_id: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    _spawn=None,
    persistent: bool = True,
) -> tuple[str, str | None]:
    """Run one Codex turn via the app-server (ChatGPT subscription).

    Returns ``(final_text, thread_id)`` where ``thread_id`` is the codex thread
    used (new or resumed) — persist it to resume on the next call.
    Raises CodexAppServerError on protocol error / timeout / non-completion.

    ``_spawn`` is injectable for tests (callable(codex_bin) -> Popen-like).
    ``persistent``: if True (default), reuse a long-lived shared server process
        across turns, respawning only when the process dies.  Pass False to
        spawn a fresh process per turn (old behaviour).
    """
    if not persistent:
        return _run_codex_turn_ephemeral(
            prompt,
            cwd=cwd,
            developer_instructions=developer_instructions,
            model=model,
            sandbox=sandbox,
            codex_bin=codex_bin,
            timeout=timeout,
            resume_thread_id=resume_thread_id,
            progress_callback=progress_callback,
            _spawn=_spawn,
        )

    # ------------------------------------------------------------------
    # Persistent path — serialised by _SHARED.lock
    # ------------------------------------------------------------------
    spawn = _spawn or _spawn_appserver

    with _SHARED.lock:
        def _attempt() -> tuple[str, str | None]:
            _SHARED.ensure(codex_bin, timeout, spawn)
            assert _SHARED.proc is not None  # noqa: S101  (ensured above)
            assert _SHARED.queue is not None  # noqa: S101

            proc = _SHARED.proc
            shared_q = _SHARED.queue

            def _send(msg: dict) -> None:
                line = json.dumps(msg, separators=(",", ":")) + "\n"
                LOG.debug("→ codex: %s", line.rstrip())
                proc.stdin.write(line)
                proc.stdin.flush()

            recv = _queue_iter(shared_q, timeout)

            def _alloc_id() -> int:
                _SHARED.next_id += 1
                return _SHARED.next_id

            session = CodexAppServerSession(
                send_fn=_send,
                recv_iter=recv,
                timeout=timeout,
                cwd=cwd,
                developer_instructions=developer_instructions,
                sandbox=sandbox,
                model=model,
                prompt=prompt,
                resume_thread_id=resume_thread_id,
                progress=progress_callback,
                skip_initialize=_SHARED.initialized,
                id_alloc=_alloc_id,
            )
            result = session.run()
            _SHARED.initialized = True
            return result, session.thread_id

        try:
            return _attempt()
        except Exception as first_exc:  # noqa: BLE001
            LOG.warning(
                "Persistent codex app-server turn failed (%s); killing and retrying once.",
                first_exc,
            )
            _SHARED.kill()
            try:
                return _attempt()
            except Exception as retry_exc:  # noqa: BLE001
                _SHARED.kill()
                raise CodexAppServerError(
                    f"codex turn failed after respawn: {retry_exc}"
                ) from retry_exc


def _run_codex_turn_ephemeral(
    prompt: str,
    *,
    cwd: str | Path,
    developer_instructions: str = "",
    model: str | None = None,
    sandbox: str = "workspace-write",
    codex_bin: str = "codex",
    timeout: float = 180.0,
    resume_thread_id: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    _spawn=None,
) -> tuple[str, str | None]:
    """Original per-turn spawn behaviour (persistent=False)."""
    spawn = _spawn or _spawn_appserver
    proc = spawn(codex_bin)

    out_q: queue.Queue = queue.Queue()
    reader_thread = _make_queue_reader(proc.stdout, out_q, timeout)
    reader_thread.start()

    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    def _send(msg: dict) -> None:
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        LOG.debug("→ codex: %s", line.rstrip())
        proc.stdin.write(line)
        proc.stdin.flush()

    recv = _queue_iter(out_q, timeout)

    session = CodexAppServerSession(
        send_fn=_send,
        recv_iter=recv,
        timeout=timeout,
        cwd=cwd,
        developer_instructions=developer_instructions,
        sandbox=sandbox,
        model=model,
        prompt=prompt,
        resume_thread_id=resume_thread_id,
        progress=progress_callback,
    )

    try:
        result = session.run()
    except CodexAppServerError:
        # Capture stderr for context
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        proc.wait(timeout=5)
        stderr_thread.join(timeout=2)
        raise
    except Exception as exc:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        proc.wait(timeout=5)
        raise CodexAppServerError(f"unexpected error: {exc}") from exc
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        reader_thread.join(timeout=3)

    return result, session.thread_id
