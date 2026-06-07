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
    
    from ..config import config_int
    # Only consider RECENTLY-active sessions — ignore ones last active long ago (e.g. days).
    recent_cutoff = max(300, config_int("jobs", "session_recent_seconds", 7200))
    idle_threshold = max(60, config_int("jobs", "session_idle_seconds", 300))

    statuses, errors = collect_fleet_status(settings, devices=(settings.status_devices or ["local"]))
    current_sessions: dict[str, SessionStatus] = {}
    for status in statuses:
        if status.age_seconds >= recent_cutoff:
            continue  # stale: last activity too long ago to care about
        current_sessions[_session_key(status)] = status

    events: list[dict[str, Any]] = []

    # (No "disappeared" event: a session aging out of the recent window is not a real
    # "finished processing" signal. The "done" idle-transition below is the true one.)

    # Check for sessions needing input and for sessions that just went idle ("done").
    for key_str, status in current_sessions.items():
        old_data = state.known_sessions.get(key_str, {})
        was_asking = old_data.get("asks_user", False)
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

        # "Done"/idle detection: a RECENT session (already filtered to the recent window)
        # that is idle past the threshold and not asking has STOPPED WORKING — notify once.
        # No "was it active first" gate: a session we only start seeing once it's already
        # idle still counts (that's the case that was silently dropped before). It re-arms
        # when the session works again, so the next stop notifies too.
        is_idle = status.age_seconds >= idle_threshold and not status.asks_user
        if not is_idle:
            notified_done = False
        if is_idle and not notified_done:
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

        # Update state
        state.known_sessions[key_str] = {
            "device_id": status.device_id,
            "runtime": status.runtime,
            "session_id": status.session_id,
            "cwd": status.cwd,
            "asks_user": status.asks_user,
            "age_seconds": status.age_seconds,
            "last_seen": now,
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


_CHANNEL_JUNK_BASENAMES = {"", "tmp", "root", "home", "bin", "src", "node_modules", "dist", "build"}


def _channel_name_for(cwd: str) -> str:
    """A valid Discord channel name (lowercase, hyphens) from a cwd's basename."""
    import os
    import re

    base = os.path.basename((cwd or "").rstrip("/")).lower()
    return re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:90]


def _is_project_cwd(cwd: str) -> bool:
    """Heuristic: is this cwd a real project worth its own channel (not /tmp, /, junk)?"""
    import os

    cwd = (cwd or "").rstrip("/")
    if not cwd or cwd == "/" or cwd.startswith("/tmp"):
        return False
    base = os.path.basename(cwd).lower()
    return base not in _CHANNEL_JUNK_BASENAMES and len(base) >= 3


async def _get_json(session, url, headers, *, attempts: int = 3):
    """GET with small retries on transient failures (so a flaky Discord call doesn't drop
    a notification). Returns parsed JSON or None."""
    import asyncio

    for i in range(attempts):
        try:
            async with session.get(url, headers=headers) as r:
                if r.status == 200:
                    return json.loads(await r.text())
                if r.status in (429, 500, 502, 503):
                    await asyncio.sleep(1 + i)
                    continue
                return None
        except Exception:
            await asyncio.sleep(1 + i)
    return None


async def _resolve_guild_and_channels(ds, session) -> tuple[str | None, dict[str, str]]:
    """Primary guild id + {lowercase channel name -> id}, fetched together with retries so
    they're always consistent (no 'channels known but guild unknown' drop)."""
    headers = {"Authorization": f"Bot {ds.token}"}
    guilds = await _get_json(session, "https://discord.com/api/v10/users/@me/guilds", headers)
    if not isinstance(guilds, list) or not guilds:
        return None, {}
    guild_id = str(guilds[0]["id"])
    name_to_id: dict[str, str] = {}
    for guild in guilds:
        chans = await _get_json(
            session, f"https://discord.com/api/v10/guilds/{guild['id']}/channels", headers
        )
        for c in chans or []:
            if c.get("type") == 0:
                name_to_id.setdefault(str(c["name"]).lower(), str(c["id"]))
    return guild_id, name_to_id


async def _create_channel(ds, session, guild_id: str, name: str) -> str | None:
    import asyncio

    headers = {"Authorization": f"Bot {ds.token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/guilds/{guild_id}/channels"
    for i in range(3):
        try:
            async with session.post(url, headers=headers, json={"name": name, "type": 0}) as r:
                if r.status in (200, 201):
                    return str(json.loads(await r.text())["id"])
                if r.status == 429:  # rate limited -> back off and retry
                    await asyncio.sleep(1 + i)
                    continue
                return None
        except Exception:
            await asyncio.sleep(1 + i)
    return None


def _resolve_channel_id(cwd: str, cwd_map: dict[str, str], name_to_id: dict[str, str]) -> str | None:
    """Pick a Discord channel id for a session cwd: first an exact/prefix match against
    configured local project channels, then the cwd's BASENAME matched against any guild
    channel (so remote sessions like ~/github/alpen/vertex-core route to #vertex-core)."""
    import os

    cwd = (cwd or "").rstrip("/")
    name = _match_channel_name(cwd, cwd_map)
    if name and name_to_id.get(name.lower()):
        return name_to_id[name.lower()]
    if cwd:
        base = os.path.basename(cwd).lower()
        for cand in (base, base.replace("_", "-"), base.replace(" ", "-")):
            if cand and cand in name_to_id:
                return name_to_id[cand]
    return None


async def _deliver_async(settings, events: list[dict[str, Any]], dm_user_id: str | None = None) -> str:
    """Deliver every event to a CHANNEL — never a DM. Project sessions go to their own
    channel (matched by cwd/basename, created if missing); anything else goes to a single
    catch-all channel ([jobs].session_fallback_channel). All channels are auto-created."""
    import aiohttp

    from bemine.discord.config import Settings
    from bemine.discord.send import send_message
    from bemine.config import config_bool, config_string

    ds = Settings.load()
    cwd_map = _project_channel_map(settings)
    fallback_name = (config_string("jobs", "session_fallback_channel", "session-updates")
                     or "session-updates").lower()
    auto_create = config_bool("jobs", "session_auto_create_channels", True)
    by_channel: dict[str, list[dict[str, Any]]] = {}
    unrouted: list[dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        guild_id, name_to_id = await _resolve_guild_and_channels(ds, session)

        async def _channel_for(cwd: str) -> str | None:
            cid = _resolve_channel_id(cwd, cwd_map, name_to_id)
            if cid or not (auto_create and guild_id):
                return cid
            # Real project -> its own channel; otherwise the catch-all channel.
            name = (_channel_name_for(cwd) if _is_project_cwd(cwd) else fallback_name) or fallback_name
            cid = name_to_id.get(name)
            if not cid:
                cid = await _create_channel(ds, session, guild_id, name)
                if cid:
                    name_to_id[name] = cid
            return cid

        for event in events:
            cid = await _channel_for(str(event.get("cwd", "")))
            if cid:
                by_channel.setdefault(cid, []).append(event)
            else:
                unrouted.append(event)

        summary: list[str] = []
        undelivered: list[dict[str, Any]] = list(unrouted)
        for channel_id, evs in by_channel.items():
            try:
                await send_message(settings=ds, channel_id=channel_id, text=render_events(evs), files=[])
                summary.append(f"channel {channel_id}: {len(evs)}")
            except Exception as exc:  # send failed -> these events must retry next poll
                undelivered.extend(evs)
                summary.append(f"channel {channel_id}: SEND FAILED ({exc})")
        if unrouted:
            summary.append(f"unrouted (no guild/channel): {len(unrouted)}")
    return ("; ".join(summary) or "no events delivered"), undelivered


def _rearm_undelivered(state_path: Path, undelivered: list[dict[str, Any]]) -> None:
    """Reset the dedup flags for events that failed to deliver, so the next poll re-fires
    them — delivery is at-least-once, never silently dropped."""
    if not undelivered:
        return
    state = WatcherState.load(state_path)
    for ev in undelivered:
        key = json.dumps({"d": ev.get("device_id"), "r": ev.get("runtime"),
                          "s": ev.get("session_id")}, sort_keys=True)
        entry = state.known_sessions.get(key)
        if not entry:
            continue
        if ev.get("type") == "done":
            entry["notified_done"] = False
        elif ev.get("type") == "needs_input":
            entry["asks_user"] = False
    state.save(state_path)


def deliver_events(settings: BemineSettings, events: list[dict[str, Any]],
                   *, dm_user_id: str | None = None, state_path: Path | None = None) -> str:
    """Route each session event to a Discord channel (project channel matched by cwd, or a
    catch-all, both auto-created). Returns a delivery summary. Undelivered events are
    re-armed (via state_path) so the next poll retries them."""
    import asyncio

    if not events:
        return ""
    summary, undelivered = asyncio.run(_deliver_async(settings, events, dm_user_id))
    if state_path is not None:
        _rearm_undelivered(state_path, undelivered)
    return summary


if __name__ == "__main__":
    settings = BemineSettings.load()
    state_path = settings.state_dir / STATE_FILE
    events = check_sessions(settings, state_path)
    
    if events:
        print(render_events(events))
    else:
        print("No session changes detected.")
