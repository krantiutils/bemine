from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bemine.discord.store import Store

DISCORD_API = "https://discord.com/api/v10"


class DiscordChannelIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChannelMetadata:
    channel_id: str
    guild_id: str
    channel_name: str
    category_name: str
    channel_type: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "channel_name": self.channel_name,
            "category_name": self.category_name,
            "channel_type": self.channel_type,
        }


def fetch_guild_channel_metadata(token: str, guild_id: str) -> list[ChannelMetadata]:
    channels = _discord_json(token, f"{DISCORD_API}/guilds/{guild_id}/channels")
    if not isinstance(channels, list):
        return []
    categories = {
        str(channel.get("id")): str(channel.get("name") or "")
        for channel in channels
        if int(channel.get("type", -1)) == 4
    }
    rows: list[ChannelMetadata] = []
    for channel in channels:
        channel_id = str(channel.get("id") or "")
        name = str(channel.get("name") or "")
        if not channel_id or not name:
            continue
        parent_id = str(channel.get("parent_id") or "")
        rows.append(
            ChannelMetadata(
                channel_id=channel_id,
                guild_id=guild_id,
                channel_name=name,
                category_name=categories.get(parent_id, ""),
                channel_type=_int_or_none(channel.get("type")),
            )
        )
    return rows


def fetch_channel_metadata(token: str, channel_id: str, *, guild_id: str = "") -> ChannelMetadata | None:
    channel = _discord_json(token, f"{DISCORD_API}/channels/{channel_id}")
    if not isinstance(channel, dict):
        return None
    name = str(channel.get("name") or "")
    if not name:
        return None
    parent_name = ""
    parent_id = str(channel.get("parent_id") or "")
    if parent_id:
        try:
            parent = _discord_json(token, f"{DISCORD_API}/channels/{parent_id}")
        except DiscordChannelIndexError:
            parent = {}
        if isinstance(parent, dict):
            parent_name = str(parent.get("name") or "")
    return ChannelMetadata(
        channel_id=str(channel.get("id") or channel_id),
        guild_id=str(channel.get("guild_id") or guild_id),
        channel_name=name,
        category_name=parent_name,
        channel_type=_int_or_none(channel.get("type")),
    )


def fetch_bot_guilds(token: str) -> list[dict[str, str]]:
    guilds = _discord_json(token, f"{DISCORD_API}/users/@me/guilds")
    if not isinstance(guilds, list):
        return []
    out: list[dict[str, str]] = []
    for guild in guilds:
        guild_id = str(guild.get("id") or "")
        name = str(guild.get("name") or "")
        if guild_id:
            out.append({"id": guild_id, "name": name})
    return out


def default_guild_id(token: str) -> str:
    guilds = fetch_bot_guilds(token)
    for guild in guilds:
        if guild["name"].strip().lower() == "voidash":
            return guild["id"]
    return guilds[0]["id"] if guilds else ""


def refresh_channel_metadata(
    store: Store,
    *,
    token: str,
    guild_id: str,
    channel_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows = fetch_guild_channel_metadata(token, guild_id)
    known = {row.channel_id for row in rows}
    for channel_id in channel_ids or []:
        if channel_id in known:
            continue
        try:
            row = fetch_channel_metadata(token, channel_id, guild_id=guild_id)
        except DiscordChannelIndexError:
            continue
        if row is not None:
            rows.append(row)
            known.add(row.channel_id)
    for row in rows:
        store.record_channel_metadata(**row.as_dict())
    return [row.as_dict() for row in rows]


def _discord_json(token: str, url: str) -> Any:
    req = Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "Accept": "application/json",
            "User-Agent": "BemineBot/1.0",
        },
    )
    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise DiscordChannelIndexError(
            f"Discord API rejected channel metadata request: HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise DiscordChannelIndexError(f"Discord API request failed: {exc.reason}") from exc


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
