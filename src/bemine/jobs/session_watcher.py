"""Session watcher job for Bemine — notifies Discord when sessions finish or need input."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bemine.config import BemineSettings
from bemine.index.status import collect_fleet_status, SessionStatus


STATE_FILE = "session_watcher_state.json"


@dataclass(frozen=True)
class SessionKey:
    device_id: str
    runtime: str
    session_id: str



@dataclass
class WatcherState:
    known_sessions: dict[str, dict[str, Any]]  # serialized SessionKey -> {last_seen, asks_user}
    last_check: float

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({
            "known_sessions": self.known_sessions,
            "last_check": self.last_check,
        }, indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> "WatcherState":
        if not path.exists():
            return cls(known_sessions={}, last_check=0)
        data = json.loads(path.read_text())
        return cls(
            known_sessions=data.get("known_sessions", {}),
            last_check=data.get("last_check", 0),
        )


def _session_key(status: SessionStatus) -> str:
    return json.dumps({"d": status.device_id, "r": status.runtime, "s": status.session_id}, sort_keys=True)


def check_sessions(settings: BemineSettings, state_path: Path) -> list[dict[str, Any]]:
    """Check sessions and return list of events to notify about."""
    state = WatcherState.load(state_path)
    now = time.time()
    
    statuses, errors = collect_fleet_status(settings, devices=["local"])
    current_sessions: dict[str, SessionStatus] = {}
    for status in statuses:
        key = _session_key(status)
        current_sessions[key] = status
    
    events: list[dict[str, Any]] = []
    
    # Check for finished sessions (were known, now gone)
    for key_str, old_data in list(state.known_sessions.items()):
        if key_str not in current_sessions:
            events.append({
                "type": "finished",
                "device_id": old_data.get("device_id", ""),
                "runtime": old_data.get("runtime", ""),
                "session_id": old_data.get("session_id", ""),
                "cwd": old_data.get("cwd", ""),
                "age_seconds": old_data.get("age_seconds", 0),
            })
    
    # Check for sessions needing input
    for key_str, status in current_sessions.items():
        old_data = state.known_sessions.get(key_str, {})
        was_asking = old_data.get("asks_user", False)
        
        if status.asks_user and not was_asking:
            events.append({
                "type": "needs_input",
                "device_id": status.device_id,
                "runtime": status.runtime,
                "session_id": status.session_id,
                "cwd": status.cwd,
                "ask_summary": status.ask_summary,
                "age_seconds": status.age_seconds,
            })
        
        # Update state
        state.known_sessions[key_str] = {
            "device_id": status.device_id,
            "runtime": status.runtime,
            "session_id": status.session_id,
            "cwd": status.cwd,
            "asks_user": status.asks_user,
            "age_seconds": status.age_seconds,
            "last_seen": now,
        }
    
    # Clean up very old finished sessions (older than 24h)
    cutoff = now - 86400
    state.known_sessions = {
        k: v for k, v in state.known_sessions.items()
        if v.get("last_seen", 0) > cutoff or k in current_sessions
    }
    
    state.last_check = now
    state.save(state_path)
    
    return events


def render_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    
    lines = ["**Session updates**"]
    for event in events:
        if event["type"] == "finished":
            lines.append(
                f"\u2705 **Finished** — `{event['session_id']}` on {event['device_id']} "
                f"(`{event['cwd'] or 'unknown'}`)"
            )
        elif event["type"] == "needs_input":
            lines.append(
                f"\u26a0\ufe0f **Needs input** — `{event['session_id']}` on {event['device_id']} "
                f"(`{event['cwd'] or 'unknown'}`)\n  > {event['ask_summary']}"
            )
    
    return "\n".join(lines)


if __name__ == "__main__":
    settings = BemineSettings.load()
    state_path = settings.state_dir / STATE_FILE
    events = check_sessions(settings, state_path)
    
    if events:
        print(render_events(events))
    else:
        print("No session changes detected.")
