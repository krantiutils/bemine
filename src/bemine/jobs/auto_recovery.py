from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bemine.config import config_bool, config_int, config_string
from bemine.jobs.project_watcher import resolve_pane

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


def classify(pane_text: str, asks_user: bool, *, signatures: list[str]) -> str:
    low = (pane_text or "").lower()
    if any(sig in low for sig in signatures):
        return "rate_limit"
    if asks_user:
        if _TRIVIAL_CONFIRM.search(pane_text or ""):
            return "trivial_confirm"
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


def recovery_key(status) -> str:
    return f"{status.device_id}:{status.runtime}:{status.session_id}"


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


@dataclass
class RecoveryDeps:
    get_statuses: Callable
    tmux: object
    decide: Callable
    escalate: Callable
    now: Callable
    sleep: Callable
    audit: Callable


def _escalation_event(status, kind: str, pane_after: str) -> dict:
    return {
        "type": "needs_input",
        "session_id": status.session_id,
        "device_id": status.device_id,
        "cwd": status.cwd,
        "ask_summary": f"auto-recovery could not clear a `{kind}` state — needs you",
        "output_tail": (pane_after or "")[-1500:],
    }


def tick_recovery(settings, deps: RecoveryDeps, *, now: float) -> None:
    sigs = _cfg_signatures()
    max_age = _cfg_max_age()
    max_attempts = _cfg_max_attempts()
    verify_delay = _cfg_verify_delay()
    base_backoff = _cfg_backoff()
    path = recovery_state_path(settings)
    state = load_recovery_state(path)
    live_keys = set()

    for status in deps.get_statuses(settings):
        if status.age_seconds > max_age:
            continue
        key = recovery_key(status)
        live_keys.add(key)
        rec = state.get(key)
        if rec and now < float(rec.get("next_retry_at", 0)):
            continue
        pane = resolve_pane(deps.tmux, status.device_id, status.cwd)
        if not pane:
            continue
        before = deps.tmux.capture(status.device_id, pane)
        kind = classify(before, status.asks_user, signatures=sigs)
        if kind == "healthy":
            state.pop(key, None)
            continue

        acted = _act(kind, deps, status, pane, before)
        if not acted:
            continue

        deps.sleep(verify_delay)
        after = deps.tmux.capture(status.device_id, pane)
        if kind == "rate_limit":
            advanced = not any(sig in after.lower() for sig in sigs)
        else:
            advanced = after != before

        rec = state.get(key) or {"attempts": 0, "backoff": base_backoff}
        if advanced:
            deps.audit("recovered", kind, status)
            state.pop(key, None)
        else:
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            rec["backoff"] = min(int(rec.get("backoff", base_backoff)) * 2, _BACKOFF_CAP)
            rec["next_retry_at"] = now + rec["backoff"]
            if rec["attempts"] >= max_attempts:
                deps.escalate(_escalation_event(status, kind, after))
                deps.audit("escalated", kind, status)
                rec["next_retry_at"] = now + _ESCALATED_HOLD
            state[key] = rec

    for key in list(state.keys()):
        if key not in live_keys:
            state.pop(key, None)
    save_recovery_state(path, state)


def _act(kind: str, deps: RecoveryDeps, status, pane: str, before: str) -> bool:
    """Perform the recovery action. Returns True if an action was issued that should be
    verified, False if nothing actionable was done (ambiguous tier deferred/escalated)."""
    if kind == "rate_limit":
        deps.tmux.send_keys(status.device_id, pane, ["Enter"])
        deps.audit("resubmit", kind, status)
        return True
    if kind == "trivial_confirm":
        deps.tmux.send_text(status.device_id, pane, "yes")
        deps.audit("auto_yes", kind, status)
        return True
    decision = deps.decide(status, before)
    if decision is None:
        return False
    dkind = getattr(decision, "kind", "wait")
    if dkind == "send_text":
        deps.tmux.send_text(status.device_id, pane, getattr(decision, "text", ""))
        deps.audit("llm_send_text", kind, status)
        return True
    if dkind == "send_keys":
        deps.tmux.send_keys(status.device_id, pane, list(getattr(decision, "keys", [])))
        deps.audit("llm_send_keys", kind, status)
        return True
    if dkind == "escalate":
        deps.escalate(_escalation_event(status, kind, before))
        deps.audit("llm_escalate", kind, status)
        return False
    return False
