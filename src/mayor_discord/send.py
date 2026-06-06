from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
from pathlib import Path

import aiohttp

from .config import Settings
from .store import Store


LOG = logging.getLogger("mayor.discord.send")
DISCORD_API = "https://discord.com/api/v10"
MAX_DISCORD_CONTENT = 3900


async def send_message(
    *,
    settings: Settings,
    channel_id: str,
    text: str,
    files: list[Path],
    reply_to: str | None = None,
) -> dict:
    settings.require_token()
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {settings.token}"}
    parts = _split_discord_content(text)

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if not files:
            result = {}
            for index, part in enumerate(parts):
                payload = _message_payload(
                    part,
                    channel_id=channel_id,
                    reply_to=reply_to if index == 0 else None,
                )
                result = await _post_json(session, url, headers, payload)
            return result

        form = aiohttp.FormData()
        payload = _message_payload(
            parts[0],
            channel_id=channel_id,
            reply_to=reply_to,
        )
        form.add_field("payload_json", json.dumps(payload))
        handles = []
        try:
            for index, path in enumerate(files):
                handle = path.open("rb")
                handles.append(handle)
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                form.add_field(
                    f"files[{index}]",
                    handle,
                    filename=path.name,
                    content_type=content_type,
                )
            async with session.post(url, headers=headers, data=form) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"Discord send failed {resp.status}: {body}")
                result = json.loads(body)
            for part in parts[1:]:
                result = await _post_json(
                    session,
                    url,
                    headers,
                    _message_payload(part, channel_id=channel_id, reply_to=None),
                )
            return result
        finally:
            for handle in handles:
                handle.close()


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    payload: dict,
) -> dict:
    async with session.post(url, headers=headers, json=payload) as resp:
        body = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"Discord send failed {resp.status}: {body}")
        return json.loads(body)


def _message_payload(text: str, *, channel_id: str, reply_to: str | None) -> dict:
    payload: dict = {
        "content": text,
        "allowed_mentions": {"parse": []},
    }
    if reply_to:
        payload["message_reference"] = {
            "message_id": reply_to,
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        }
    return payload


def chunk_for_discord(text: str, *, limit: int = 2000) -> list[str]:
    """Split text into <=limit pieces, preferring newline boundaries, never exceeding limit.

    Concatenation of the chunks reproduces the input exactly (no characters dropped).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


def _split_discord_content(text: str, *, limit: int = MAX_DISCORD_CONTENT) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current or not chunks:
        chunks.append(current.rstrip())
    return chunks


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a Discord message from Mayor.")
    parser.add_argument("--text", default="", help="Message text.")
    parser.add_argument("--file", action="append", default=[], help="File to attach. Can repeat.")
    parser.add_argument("--channel", default="", help="Discord channel ID. Defaults to home channel.")
    parser.add_argument("--reply-to", default="", help="Discord message ID to reply to.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    settings = Settings.load()
    channel_id = args.channel.strip() or settings.home_channel_id
    if not channel_id:
        settings.require_home_channel()

    files = [Path(raw).expanduser().resolve() for raw in args.file]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise SystemExit(f"File not found: {', '.join(missing)}")
    if not args.text and not files:
        raise SystemExit("Nothing to send. Use --text and/or --file.")

    result = asyncio.run(
        send_message(
            settings=settings,
            channel_id=channel_id,
            text=args.text,
            files=files,
            reply_to=args.reply_to.strip() or None,
        )
    )
    Store(settings.db_path).record_outbound(
        discord_message_id=str(result.get("id") or ""),
        channel_id=channel_id,
        content=args.text,
        files=files,
    )
    print(json.dumps({"ok": True, "id": result.get("id"), "channel_id": channel_id}))


if __name__ == "__main__":
    main()
