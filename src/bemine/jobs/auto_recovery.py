from __future__ import annotations

import hashlib
import json
import re
import time
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

# Plan/usage-window limits (e.g. codex's 5h cap): resending is pointless — wait for the
# stated reset time, then nudge with "continue". Checked BEFORE rate-limit signatures.
USAGE_LIMIT_DEFAULTS = [
    "usage limit",
    "hit your limit",
    "reached your limit",
    "limit will reset",
    "limit resets",
    "try again at",
    "5-hour limit",
    "5 hour limit",
    "weekly limit",
]

# "… try again at 3:47am" / "resets at 03:47" — capture the clock time of the reset.
_RESET_TIME = re.compile(
    r"(?:try again|resets?|reset)\D{0,15}?(\d{1,2}):(\d{2})\s*(am|pm)?",
    re.IGNORECASE,
)


def _parse_reset_epoch(text: str, now: float) -> float | None:
    """Epoch of the next occurrence of the reset clock-time stated in *text* (local tz)."""
    m = _RESET_TIME.search(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    lt = time.localtime(now)
    candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0,
                             0, 0, -1))
    if candidate <= now + 60:
        candidate += 86400
    return candidate

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

_RECENT_LINES = 30


def _recent(text: str) -> str:
    return "\n".join((text or "").splitlines()[-_RECENT_LINES:])


def classify(pane_text: str, asks_user: bool = False, *, signatures: list[str]) -> str:
    text = _recent(pane_text)
    low = text.lower()
    # "(not your usage limit)" in the transient rate-limit message must not trip this tier.
    usage_low = low.replace("not your usage limit", "")
    if any(sig in usage_low for sig in USAGE_LIMIT_DEFAULTS):
        return "usage_limit"
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
_LIMIT_RETRY_CAP = 600   # default post-reset "continue" retry cap: 10 min


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


def _cfg_poll_interval() -> int:
    """How often the daemon runs a recovery pass (it polls more often than this)."""
    return config_int("recovery", "poll_interval_secs", 300)


def _cfg_usage_limit_retry_cap() -> int:
    return max(60, config_int("recovery", "usage_limit_retry_cap_secs", _LIMIT_RETRY_CAP))


def _cfg_signatures() -> list[str]:
    raw = config_string("recovery", "rate_limit_signatures", "")
    if raw.strip():
        return [s.strip().lower() for s in raw.split(",") if s.strip()]
    return RATE_LIMIT_DEFAULTS


# Pane commands that are NOT agent sessions (shells/editors/pagers/VCS/etc.). Every OTHER
# pane command is treated as a potential agent session — an allowlist of names fails because
# Claude Code reports its VERSION (e.g. "2.1.168") as the pane's current command, and codex
# shows "codex-aarch64-a". classify() gates action, so capturing a non-agent pane is harmless.
_NON_AGENT_COMMANDS = {
    "bash", "-bash", "zsh", "-zsh", "sh", "-sh", "fish", "-fish", "tmux", "vim", "nvim",
    "vi", "emacs", "nano", "less", "more", "man", "ssh", "mosh", "tail", "head", "cat",
    "grep", "rg", "htop", "top", "btop", "watch", "git", "fzf", "make", "lazygit", "gitui",
}


def _cfg_non_agent_commands() -> set[str]:
    raw = config_string("recovery", "non_agent_commands", "")
    if raw.strip():
        return {c.strip().lower() for c in raw.split(",") if c.strip()}
    return _NON_AGENT_COMMANDS


def _is_agent_pane(command: str) -> bool:
    """True if a tmux pane's current command looks like an agent session (not a shell/editor)."""
    cmd = (command or "").strip().lower()
    if not cmd or cmd.startswith("-"):  # empty or a login shell
        return False
    return cmd not in _cfg_non_agent_commands()


@dataclass
class RecoveryDeps:
    get_panes: Callable      # (settings) -> list[dict]; each {device,pane_id,path,command,activity}
    tmux: object
    escalate: Callable
    now: Callable
    sleep: Callable
    audit: Callable
    decide: Callable = None   # (pane, pane_text) -> Decision|None ; None disables the LLM tier


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
    base_backoff = _cfg_backoff()
    path = recovery_state_path(settings)
    state = load_recovery_state(path)
    live = set()

    for pane in deps.get_panes(settings):
        try:
            key = recovery_key(pane)
            live.add(key)
            activity = int(pane.get("activity") or 0)
            if activity and (now - activity) > max_age:
                continue
            rec = state.get(key) or {}
            if rec.get("next_retry_at") and now < float(rec["next_retry_at"]):
                state[key] = rec
                continue
            device = pane["device"]
            pid = pane["pane_id"]
            before = deps.tmux.capture(device, pid)
            kind = classify(before, signatures=sigs)

            if kind == "usage_limit":
                state[key] = _handle_usage_limit(deps, pane, device, pid, rec, now, before)
                continue

            if kind == "rate_limit":
                state[key] = _handle_rate_limit(deps, pane, device, pid, rec, now,
                                                base_backoff, max_attempts)
                continue

            # No longer rate/usage-limited: if we were mid episode, it cleared.
            if rec.get("rl_attempts") or rec.get("limit_wait_until"):
                if rec.get("rl_attempts") and not rec.get("escalated"):
                    deps.audit("recovered", "rate_limit", pane)
                if rec.get("limit_wait_until"):
                    deps.audit("recovered", "usage_limit", pane)
                state.pop(key, None)
                continue

            if kind == "trivial_confirm":
                deps.tmux.send_text(device, pid, "yes")
                deps.audit("auto_yes", "trivial_confirm", pane)
                state.pop(key, None)  # a later tick confirms it advanced
                continue

            # Otherwise: maybe stuck on a question. Idle-gated, deduped LLM tier.
            h = hashlib.sha1(_recent(before).encode("utf-8", "ignore")).hexdigest()
            if rec.get("seen_hash") == h:
                if rec.get("llm_done"):
                    state[key] = {"seen_hash": h, "llm_done": True}
                    continue
                _handle_question(deps, pane, device, pid, before)
                state[key] = {"seen_hash": h, "llm_done": True}
            else:
                state[key] = {"seen_hash": h, "llm_done": False}
        except Exception:
            continue

    for k in list(state.keys()):
        if k not in live:
            state.pop(k, None)
    save_recovery_state(path, state)


def _fallback_reset_epoch(now: float) -> float:
    """Next occurrence of [recovery].limit_reset_time (default 03:47, the codex window)."""
    raw = config_string("recovery", "limit_reset_time", "03:47")
    try:
        hour, minute = (int(p) for p in raw.strip().split(":"))
    except ValueError:
        hour, minute = 3, 47
    lt = time.localtime(now)
    candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if candidate <= now + 60:
        candidate += 86400
    return candidate


def _handle_rate_limit(deps, pane, device, pid, rec, now, base_backoff, max_attempts) -> dict:
    if rec.get("escalated"):
        # Already pinged the human for this episode — hold silently, no more resends.
        rec["next_retry_at"] = now + _ESCALATED_HOLD
        return rec
    attempts = int(rec.get("rl_attempts", 0)) + 1
    deps.tmux.send_keys(device, pid, ["Up", "Enter"])  # recall + resend previous message
    deps.audit("resubmit", "rate_limit", pane)
    backoff = min(int(rec.get("backoff", base_backoff)) * 2, _BACKOFF_CAP)
    new = {"rl_attempts": attempts, "backoff": backoff,
           "next_retry_at": now + backoff, "escalated": bool(rec.get("escalated"))}
    if attempts >= max_attempts and not new["escalated"]:
        deps.escalate(_escalation_event(pane, "rate_limit", ""))
        deps.audit("escalated", "rate_limit", pane)
        new["escalated"] = True
    if attempts >= max_attempts:
        new["next_retry_at"] = now + _ESCALATED_HOLD
    return new


def _handle_usage_limit(deps, pane, device, pid, rec, now, pane_text) -> dict:
    wait_until = float(rec.get("limit_wait_until") or 0)
    if wait_until <= 0:
        # Prefer the reset time stated on screen; else the configured window reset
        # ([recovery].limit_reset_time, default 03:47 — the codex 5h window).
        wait_until = _parse_reset_epoch(pane_text, now) or _fallback_reset_epoch(now)

    if now < wait_until:
        if not rec.get("limit_wait_audited"):
            deps.audit("limit_wait", "usage_limit", pane)
        return {
            "limit_wait_until": wait_until,
            "next_retry_at": wait_until,
            "limit_wait_audited": True,
            "limit_continue_sent": bool(rec.get("limit_continue_sent")),
        }

    attempts = int(rec.get("continue_attempts", 0))

    # A nudge already went out and the pane STILL shows a limit: the wait was probably
    # mis-scheduled (each session's 5h window resets at its own time, the fallback can be
    # wrong). If the pane itself states a reset comfortably in the future, RE-WAIT to it
    # instead of poking the session for hours.
    if attempts:
        stated = _parse_reset_epoch(pane_text, now)
        if stated and stated > now + 120:
            st_lt, w_lt = time.localtime(stated), time.localtime(wait_until)
            # Same clock-time as the reset we already waited for = stale text, keep
            # retrying. A DIFFERENT stated time is new information -> re-wait to it.
            if (st_lt.tm_hour, st_lt.tm_min) != (w_lt.tm_hour, w_lt.tm_min):
                deps.audit("limit_wait", "usage_limit", pane)
                return {
                    "limit_wait_until": stated,
                    "next_retry_at": stated,
                    "limit_wait_audited": True,
                }

    # Past the reset: nudge "continue", and if the pane still shows the limit on later
    # passes, RETRY with doubling backoff capped at 10 minutes — never stall to the next
    # day on a nudge that didn't take. When the pane moves, classify() stops returning
    # usage_limit and the recovered path clears this record.
    deps.tmux.send_text(device, pid, "continue")
    deps.audit("limit_continue", "usage_limit", pane)
    backoff = min(60 * (2 ** attempts), _cfg_usage_limit_retry_cap())
    return {
        "limit_wait_until": wait_until,
        "next_retry_at": now + backoff,
        "limit_wait_audited": True,
        "continue_attempts": attempts + 1,
    }


def _handle_question(deps, pane, device, pid, before) -> None:
    decision = deps.decide(pane, before) if deps.decide else None
    if decision is None:
        return
    kind = getattr(decision, "kind", "wait")
    if kind == "send_text" and getattr(decision, "text", "").strip():
        deps.tmux.send_text(device, pid, decision.text)
        deps.audit("llm_send_text", "question", pane)
    elif kind == "send_keys" and getattr(decision, "keys", None):
        deps.tmux.send_keys(device, pid, list(decision.keys))
        deps.audit("llm_send_keys", "question", pane)
    elif kind == "escalate":
        deps.escalate(_escalation_event(pane, "question", before))
        deps.audit("llm_escalate", "question", pane)
    # "wait"/None -> do nothing


def default_recovery_deps(settings) -> RecoveryDeps:
    import time
    from bemine.jobs.remote_tmux import RemoteTmux
    from bemine.jobs.session_watcher import deliver_events
    from bemine.db import BemineDB
    from bemine.index.status import DEFAULT_STATUS_DEVICES

    tmux = RemoteTmux(gateway_device=settings.device_id)

    def _panes(s):
        devices = list(s.status_devices) if s.status_devices else list(DEFAULT_STATUS_DEVICES)
        out = []
        for dev in devices:
            try:
                for p in tmux.list_panes(dev):
                    if _is_agent_pane(p.get("command")):
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

    from bemine.jobs.project_watcher import decide as _watcher_decide
    from bemine.discord.dispatch import _DEFAULT_MODELS
    from bemine.agent.clients import build_client_for_runtime

    _BRIEF = (
        "A terminal AGENT session (claude/codex/kimi) is shown. The error/message may be a "
        "few lines ABOVE the input box (the box + hints render below it) — scan the whole "
        "screen, not just the last line. Choose ONE action to get it moving again, SAFELY:\n"
        "- TRANSIENT API/SERVER ERROR — if anywhere on screen you see 'API Error', 'rate "
        "limited', 'temporarily limiting requests', 'overloaded', 'server busy', 5xx, etc., "
        "AND the agent is now sitting idle at its input prompt, the turn FAILED: resend the "
        "previous message with keys [\"Up\",\"Enter\"]. An idle prompt right after such an "
        "error is NOT 'done' — do NOT choose wait; resend it.\n"
        "- QUESTION — if it is asking something you can answer to continue, reply with the "
        "text to send.\n"
        "- NEEDS A HUMAN — model 'at capacity'/'try a different model'/unavailable, or it "
        "needs a real decision, a credential, a model switch, or any destructive/irreversible "
        "action: do NOT guess, escalate with a short question.\n"
        "- Only choose wait if it is genuinely still working/generating, or truly finished "
        "with no error and no pending question.\n"
        "Never run destructive or irreversible commands."
    )

    def _runtime_for(cmd: str) -> str:
        c = (cmd or "").lower()
        if "codex" in c:
            return "codex"
        if "kimi" in c:
            return "kimi"
        return "claude"  # claude/node/default

    def _decide(pane, pane_text):
        try:
            rt = _runtime_for(pane.get("command", ""))
            client = build_client_for_runtime(settings, rt)
            model = config_string(rt, "model", "") or _DEFAULT_MODELS.get(rt, "kimi-k2.6")
            return _watcher_decide(client, model=model, brief=_BRIEF,
                                   pane_capture=pane_text, transcript_tail="",
                                   ask_summary="", channel_context="")
        except Exception:
            return None

    return RecoveryDeps(get_panes=_panes, tmux=tmux, escalate=_escalate,
                        now=time.time, sleep=time.sleep, audit=_audit, decide=_decide)
