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
        self._next_id = 0
        self.thread_id: str | None = None

    def _rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def run(self) -> str:
        """Execute the full handshake and return the final agent text."""
        # Step 1: initialize
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
                elif self._progress and method == "item/started":
                    label = (
                        item.get("command")
                        or item.get("path")
                        or item.get("query")
                        or itype
                    )
                    try:
                        self._progress(f"codex: {str(label)[:120]}")
                    except Exception:  # noqa: BLE001
                        pass

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
                    continue
                raise CodexAppServerError(
                    f"codex notification error ({method}): {msg['params']['error']}"
                )

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
) -> tuple[str, str | None]:
    """Run one Codex turn via the app-server (ChatGPT subscription).

    Returns ``(final_text, thread_id)`` where ``thread_id`` is the codex thread
    used (new or resumed) — persist it to resume on the next call.
    Raises CodexAppServerError on protocol error / timeout / non-completion.

    ``_spawn`` is injectable for tests (callable(codex_bin) -> Popen-like).
    """
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
