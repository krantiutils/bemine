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

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if not files:
            async with session.post(url, headers=headers, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"Discord send failed {resp.status}: {body}")
                return json.loads(body)

        form = aiohttp.FormData()
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
                return json.loads(body)
        finally:
            for handle in handles:
                handle.close()


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
