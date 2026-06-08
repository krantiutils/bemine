from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.discord.config import Settings as DiscordSettings
from bemine.discord.send import send_message
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
        deps.echo(channel_id, f"\U0001f64b `{project_name}` needs you: {decision.question}")
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
