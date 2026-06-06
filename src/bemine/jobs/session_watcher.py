"""Session watcher job for Bemine — notifies Discord when sessions finish or need input."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bemine.config import BemineSettings
from bemine.index.status import collect_fleet_status, SessionStatus, session_transcript_tail


def _output_tail(status: "SessionStatus") -> str:
    """Last ~30 lines of the session's output (redacted), for the notification."""
    if not status.source_path:
        return ""
    try:
        return session_transcript_tail(Path(status.source_path), status.runtime)
    except Exception:
        return ""


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
    
    statuses, errors = collect_fleet_status(settings, devices=(settings.status_devices or ["local"]))
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
    
    # An idle threshold: a session whose last activity is older than this is "done
    # working". Configurable via [jobs].session_idle_seconds.
    from ..config import config_int
    idle_threshold = max(60, config_int("jobs", "session_idle_seconds", 300))

    # Check for sessions needing input and for sessions that just went idle ("done").
    for key_str, status in current_sessions.items():
        old_data = state.known_sessions.get(key_str, {})
        was_asking = old_data.get("asks_user", False)
        seen_active = old_data.get("seen_active", False)
        notified_done = old_data.get("notified_done", False)

        if status.asks_user and not was_asking:
            events.append({
                "type": "needs_input",
                "device_id": status.device_id,
                "runtime": status.runtime,
                "session_id": status.session_id,
                "cwd": status.cwd,
                "ask_summary": status.ask_summary,
                "age_seconds": status.age_seconds,
                "output_tail": _output_tail(status),
            })

        # "Done" detection: we must have OBSERVED the session actively working (recent
        # activity) before we'll announce it finished — this avoids spamming old idle
        # sessions on first sight. When an observed-active session crosses the idle
        # threshold (and isn't asking), fire one "done" event.
        active_now = status.age_seconds < idle_threshold and not status.asks_user
        if active_now:
            seen_active = True
            notified_done = False
        if seen_active and not active_now and not status.asks_user and not notified_done:
            events.append({
                "type": "done",
                "device_id": status.device_id,
                "runtime": status.runtime,
                "session_id": status.session_id,
                "cwd": status.cwd,
                "latest_summary": status.latest_summary,
                "age_seconds": status.age_seconds,
                "output_tail": _output_tail(status),
            })
            notified_done = True
            seen_active = False

        # Update state
        state.known_sessions[key_str] = {
            "device_id": status.device_id,
            "runtime": status.runtime,
            "session_id": status.session_id,
            "cwd": status.cwd,
            "asks_user": status.asks_user,
            "age_seconds": status.age_seconds,
            "last_seen": now,
            "seen_active": seen_active,
            "notified_done": notified_done,
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
        if event["type"] == "done":
            summary = str(event.get("latest_summary") or "").strip()
            tail = ("\n  > " + summary) if summary else ""
            lines.append(
                "✅ **Done** — `" + str(event.get("runtime", "")) + "` finished working in `"
                + str(event.get("cwd") or "unknown") + "` on " + str(event.get("device_id", "")) + tail
            )
        elif event["type"] == "finished":
            lines.append(
                f"\u2705 **Finished** — `{event['session_id']}` on {event['device_id']} "
                f"(`{event['cwd'] or 'unknown'}`)"
            )
        elif event["type"] == "needs_input":
            lines.append(
                f"\u26a0\ufe0f **Needs input** — `{event['session_id']}` on {event['device_id']} "
                f"(`{event['cwd'] or 'unknown'}`)\n  > {event['ask_summary']}"
            )
        # Recent output so you can read what happened without opening the terminal.
        out_tail = str(event.get("output_tail") or "").strip()
        if out_tail:
            lines.append("```\n" + out_tail[:1600] + "\n```")

    return "\n".join(lines)


def _project_channel_map(settings: BemineSettings) -> dict[str, str]:
    """cwd -> project channel name, from .bemine/discord-projects.json."""
    path = settings.state_dir / "discord-projects.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for entry in data.values():
        if isinstance(entry, dict) and entry.get("cwd") and entry.get("channel_name"):
            out[str(entry["cwd"]).rstrip("/")] = str(entry["channel_name"])
    return out


def _match_channel_name(cwd: str, cwd_map: dict[str, str]) -> str | None:
    """Map a session cwd to a project channel name (exact, then longest-prefix match)."""
    cwd = (cwd or "").rstrip("/")
    if not cwd:
        return None
    best: str | None = None
    best_len = -1
    for pcwd, name in cwd_map.items():
        if (cwd == pcwd or cwd.startswith(pcwd + "/")) and len(pcwd) > best_len:
            best, best_len = name, len(pcwd)
    return best


async def _resolve_guild_channels(settings, session) -> dict[str, str]:
    """Lowercased channel name -> channel id across the bot's guilds (text channels)."""
    import aiohttp  # noqa: F401  (session is an aiohttp session)

    base = "https://discord.com/api/v10"
    headers = {"Authorization": f"Bot {settings.token}"}
    name_to_id: dict[str, str] = {}
    async with session.get(f"{base}/users/@me/guilds", headers=headers) as r:
        guilds = json.loads(await r.text())
    if not isinstance(guilds, list):
        return name_to_id
    for guild in guilds:
        async with session.get(f"{base}/guilds/{guild['id']}/channels", headers=headers) as r:
            chans = json.loads(await r.text())
        if not isinstance(chans, list):
            continue
        for c in chans:
            if c.get("type") == 0:  # text channel
                name_to_id.setdefault(str(c["name"]).lower(), str(c["id"]))
    return name_to_id


async def _deliver_async(settings, events: list[dict[str, Any]], dm_user_id: str | None) -> str:
    import aiohttp

    from bemine.discord.config import Settings
    from bemine.discord.send import send_message, open_dm_channel

    ds = Settings.load()
    cwd_map = _project_channel_map(settings)
    by_channel: dict[str, list[dict[str, Any]]] = {}
    dm_events: list[dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        name_to_id = await _resolve_guild_channels(ds, session)
        for event in events:
            channel_name = _match_channel_name(str(event.get("cwd", "")), cwd_map)
            channel_id = name_to_id.get((channel_name or "").lower()) if channel_name else None
            if channel_id:
                by_channel.setdefault(channel_id, []).append(event)
            else:
                dm_events.append(event)

        summary: list[str] = []
        for channel_id, evs in by_channel.items():
            await send_message(settings=ds, channel_id=channel_id, text=render_events(evs), files=[])
            summary.append(f"channel {channel_id}: {len(evs)}")
        if dm_events and dm_user_id:
            dm_id = await open_dm_channel(ds, str(dm_user_id))
            await send_message(settings=ds, channel_id=dm_id, text=render_events(dm_events), files=[])
            summary.append(f"DM: {len(dm_events)}")
        elif dm_events:
            summary.append(f"unrouted (no DM target): {len(dm_events)}")
    return "; ".join(summary) or "no events delivered"


def deliver_events(settings: BemineSettings, events: list[dict[str, Any]],
                   *, dm_user_id: str | None = None) -> str:
    """Route each session event to its project's Discord channel (matched by cwd), with a
    DM fallback for sessions that don't map to a channel. Returns a delivery summary."""
    import asyncio

    if not events:
        return ""
    return asyncio.run(_deliver_async(settings, events, dm_user_id))


if __name__ == "__main__":
    settings = BemineSettings.load()
    state_path = settings.state_dir / STATE_FILE
    events = check_sessions(settings, state_path)
    
    if events:
        print(render_events(events))
    else:
        print("No session changes detected.")
