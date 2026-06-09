from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bemine.config import config_bool, config_int, config_string

RATE_LIMIT_DEFAULTS = [
    "server is temporarily limiting requests",
    "rate limited",
    "rate_limit",
    "api error",
    "overloaded",
    "529",
    "too many requests",
    "please try again",
]

# Obvious "just say yes / press enter" prompts that are safe to auto-confirm.
_TRIVIAL_CONFIRM = re.compile(
    r"\(y/n\)|\[y/n\]|yes/no|y/n\)|proceed\?|continue\?|press enter|"
    r"do you want to (proceed|continue)|confirm\?|\bok\?\b",
    re.IGNORECASE,
)

# A confirm prompt mentioning any of these is NOT safe to auto-yes — route to the LLM tier.
_DESTRUCTIVE = re.compile(
    r"\b(delete|deletes|deleting|remove|removes|removing|\brm\b|force[- ]?push|"
    r"reset\s+--hard|drop\s|overwrite|destroy|terminate|wipe|truncate|format\s|"
    r"revoke|uninstall|tear\s?down|purge|prune)\b",
    re.IGNORECASE,
)


def classify(pane_text: str, asks_user: bool = False, *, signatures: list[str]) -> str:
    text = pane_text or ""
    low = text.lower()
    if any(sig in low for sig in signatures):
        return "rate_limit"
    if _TRIVIAL_CONFIRM.search(text) and not _DESTRUCTIVE.search(text):
        return "trivial_confirm"
    if asks_user:
        return "ambiguous"
    return "healthy"


def recovery_state_path(settings) -> Path:
    return settings.state_dir / "recovery_state.json"


def load_recovery_state(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_recovery_state(path: Path, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def recovery_key(pane: dict) -> str:
    return f"{pane['device']}:{pane['pane_id']}"


_BACKOFF_CAP = 1800
_ESCALATED_HOLD = 86400  # after escalation, don't retry for a day (until state changes)


def recovery_enabled() -> bool:
    return config_bool("recovery", "enabled", True)


def _cfg_max_age() -> int:
    return config_int("recovery", "max_session_age_secs", 1800)


def _cfg_backoff() -> int:
    return config_int("recovery", "backoff_secs", 30)


def _cfg_max_attempts() -> int:
    return config_int("recovery", "max_attempts", 5)


def _cfg_verify_delay() -> int:
    return config_int("recovery", "verify_delay_secs", 4)


def _cfg_signatures() -> list[str]:
    raw = config_string("recovery", "rate_limit_signatures", "")
    if raw.strip():
        return [s.strip().lower() for s in raw.split(",") if s.strip()]
    return RATE_LIMIT_DEFAULTS


def _cfg_agent_commands() -> list[str]:
    raw = config_string("recovery", "agent_commands", "")
    if raw.strip():
        return [c.strip().lower() for c in raw.split(",") if c.strip()]
    return ["claude", "codex", "kimi", "node", "python"]


@dataclass
class RecoveryDeps:
    get_panes: Callable      # (settings) -> list[dict]; each {device,pane_id,path,command,activity}
    tmux: object
    escalate: Callable
    now: Callable
    sleep: Callable
    audit: Callable


def _escalation_event(pane: dict, kind: str, pane_after: str) -> dict:
    return {
        "type": "needs_input",
        "session_id": pane.get("pane_id", ""),
        "device_id": pane.get("device", ""),
        "runtime": pane.get("command", ""),
        "cwd": pane.get("path", ""),
        "ask_summary": f"auto-recovery could not clear a `{kind}` state — needs you",
        "output_tail": (pane_after or "")[-1500:],
    }


def tick_recovery(settings, deps: "RecoveryDeps", *, now: float) -> None:
    sigs = _cfg_signatures()
    max_age = _cfg_max_age()
    max_attempts = _cfg_max_attempts()
    verify_delay = _cfg_verify_delay()
    base_backoff = _cfg_backoff()
    path = recovery_state_path(settings)
    state = load_recovery_state(path)
    live_keys = set()

    for pane in deps.get_panes(settings):
        try:
            key = recovery_key(pane)
            live_keys.add(key)
            activity = int(pane.get("activity") or 0)
            if activity and (now - activity) > max_age:
                continue
            rec = state.get(key)
            if rec and now < float(rec.get("next_retry_at", 0)):
                continue
            device = pane["device"]
            pid = pane["pane_id"]
            before = deps.tmux.capture(device, pid)
            kind = classify(before, signatures=sigs)
            if kind == "healthy":
                state.pop(key, None)
                continue

            if not _act(kind, deps, pane, before):
                continue

            deps.sleep(verify_delay)
            after = deps.tmux.capture(device, pid)
            if kind == "rate_limit":
                advanced = not any(sig in after.lower() for sig in sigs)
            else:
                advanced = after != before

            rec = state.get(key) or {"attempts": 0, "backoff": base_backoff}
            if advanced:
                deps.audit("recovered", kind, pane)
                state.pop(key, None)
            else:
                rec["attempts"] = int(rec.get("attempts", 0)) + 1
                rec["backoff"] = min(int(rec.get("backoff", base_backoff)) * 2, _BACKOFF_CAP)
                rec["next_retry_at"] = now + rec["backoff"]
                if rec["attempts"] >= max_attempts and not rec.get("escalated"):
                    deps.escalate(_escalation_event(pane, kind, after))
                    deps.audit("escalated", kind, pane)
                    rec["escalated"] = True
                if rec["attempts"] >= max_attempts:
                    rec["next_retry_at"] = now + _ESCALATED_HOLD
                state[key] = rec
        except Exception:
            continue

    for key in list(state.keys()):
        if key not in live_keys:
            state.pop(key, None)
    save_recovery_state(path, state)


def _act(kind: str, deps: "RecoveryDeps", pane: dict, before: str) -> bool:
    device = pane["device"]
    pid = pane["pane_id"]
    if kind == "rate_limit":
        deps.tmux.send_keys(device, pid, ["Enter"])
        deps.audit("resubmit", kind, pane)
        return True
    if kind == "trivial_confirm":
        deps.tmux.send_text(device, pid, "yes")
        deps.audit("auto_yes", kind, pane)
        return True
    return False


def default_recovery_deps(settings) -> RecoveryDeps:
    import time
    from bemine.jobs.remote_tmux import RemoteTmux
    from bemine.jobs.session_watcher import deliver_events
    from bemine.db import BemineDB
    from bemine.index.status import DEFAULT_STATUS_DEVICES

    tmux = RemoteTmux(gateway_device=settings.device_id)
    agent_cmds = _cfg_agent_commands()

    def _panes(s):
        devices = list(s.status_devices) if s.status_devices else list(DEFAULT_STATUS_DEVICES)
        out = []
        for dev in devices:
            try:
                for p in tmux.list_panes(dev):
                    cmd = (p.get("command") or "").lower()
                    if any(a in cmd for a in agent_cmds):
                        out.append({**p, "device": dev})
            except Exception:
                continue
        return out

    def _escalate(event):
        try:
            deliver_events(settings, [event])
        except Exception:
            pass

    def _audit(event_type, kind, pane):
        try:
            BemineDB(settings.db_path).record_audit_event(
                category="recovery", event_type=event_type, actor="auto_recovery",
                device_id=pane.get("device"),
                payload_json=json.dumps({"kind": kind, "pane": pane.get("pane_id"),
                                         "cwd": pane.get("path"), "command": pane.get("command")}),
            )
        except Exception:
            pass

    return RecoveryDeps(get_panes=_panes, tmux=tmux, escalate=_escalate,
                        now=time.time, sleep=time.sleep, audit=_audit)
