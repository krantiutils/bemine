from __future__ import annotations

import asyncio
import argparse
import logging
import re
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

from bemine.config import BemineSettings
from bemine.kernel import BemineKernel

from .config import Settings
from .store import Store


LOG = logging.getLogger("mayor.discord.gateway")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
VOICE_SUFFIXES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm"}


def _safe_filename(name: str) -> str:
    cleaned = SAFE_FILENAME_RE.sub("_", Path(name).name).strip("._")
    return cleaned or "attachment"


def _is_voice_attachment(attachment: discord.Attachment) -> bool:
    flags = getattr(attachment, "flags", None)
    if bool(getattr(flags, "voice", False)):
        return True
    content_type = (attachment.content_type or "").lower()
    suffix = Path(attachment.filename or "").suffix.lower()
    return content_type.startswith("audio/") or suffix in VOICE_SUFFIXES


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("image/")


class MayorDiscordGateway:
    def __init__(self, settings: Settings) -> None:
        settings.require_token()
        self.settings = settings
        self.store = Store(settings.db_path)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True

        self.bot = commands.Bot(
            command_prefix="!mayor ",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
                replied_user=True,
            ),
        )
        self._register_events()

    def _register_events(self) -> None:
        @self.bot.event
        async def on_ready() -> None:
            LOG.info("connected as %s", self.bot.user)
            LOG.info("state db: %s", self.settings.db_path)
            LOG.info("inbox: %s", self.settings.inbox_dir)

        @self.bot.event
        async def on_message(message: discord.Message) -> None:
            if self.bot.user and message.author.id == self.bot.user.id:
                return
            if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
                return
            if not self._is_allowed(message.author):
                LOG.warning("ignored unauthorized Discord user %s", message.author.id)
                return

            saved_attachments = await self._record_message(message)

            if message.content.strip().lower() in {"!mayor ping", "mayor ping"}:
                await message.reply("Mayor gateway online.", mention_author=False)
                return
            if message.content.strip().lower() in {"!mayor status", "mayor status"}:
                stats = self.store.stats()
                await message.reply(
                    "Mayor gateway online.\n"
                    f"Inbound: {stats['inbound_messages']}\n"
                    f"Attachments: {stats['inbound_attachments']}\n"
                    f"Outbound: {stats['outbound_messages']}",
                    mention_author=False,
                )
                return
            if message.content.strip().lower() in {
                "!mayor rundown",
                "mayor rundown",
                "!bemine rundown",
                "bemine rundown",
            }:
                async with message.channel.typing():
                    kernel = BemineKernel(BemineSettings.load())
                    kernel.scan_local()
                    rundown = kernel.project_rundown(limit=12)
                await message.reply(
                    _discord_chunk(rundown, limit=1900)[0],
                    mention_author=False,
                )
                return

            await self.bot.process_commands(message)
            await message.reply(
                self._default_reply(saved_attachments),
                mention_author=False,
            )

    def _is_allowed(self, author: discord.abc.User) -> bool:
        if self.settings.allow_all_users:
            return True
        return str(author.id) in self.settings.allowed_users

    def _default_reply(self, saved_attachments: list[Path]) -> str:
        if saved_attachments:
            return (
                "Received by Mayor. "
                f"Saved {len(saved_attachments)} attachment(s). "
                "Codex dispatch is not wired yet."
            )
        return "Received by Mayor. Codex dispatch is not wired yet."

    async def _record_message(self, message: discord.Message) -> list[Path]:
        message_id = str(message.id)
        channel_id = str(message.channel.id)
        guild_id = str(message.guild.id) if message.guild else None
        is_dm = isinstance(message.channel, discord.DMChannel) or message.guild is None
        author_name = getattr(message.author, "global_name", None) or str(message.author)
        saved_attachments: list[Path] = []

        self.store.record_inbound_message(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
            author_id=str(message.author.id),
            author_name=author_name,
            content=message.content or "",
            is_dm=is_dm,
            created_at=message.created_at.isoformat(),
        )

        for attachment in message.attachments:
            saved_path = await self._save_attachment(message, attachment)
            saved_attachments.append(saved_path)
            self.store.record_attachment(
                message_id=message_id,
                attachment_id=str(attachment.id),
                filename=attachment.filename or "",
                content_type=attachment.content_type,
                size=attachment.size,
                url=attachment.url,
                saved_path=saved_path,
                is_image=_is_image_attachment(attachment),
                is_voice=_is_voice_attachment(attachment),
            )
            LOG.info("saved attachment %s -> %s", attachment.filename, saved_path)
        return saved_attachments

    async def _save_attachment(
        self,
        message: discord.Message,
        attachment: discord.Attachment,
    ) -> Path:
        date_dir = datetime.now().strftime("%Y%m%d")
        target_dir = self.settings.inbox_dir / date_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(attachment.filename or f"{attachment.id}.bin")
        target = target_dir / f"{message.id}_{attachment.id}_{filename}"
        await attachment.save(target)
        return target

    async def run(self) -> None:
        await self.bot.start(self.settings.token)


def _discord_chunk(text: str, *, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [text[:limit]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Mayor Discord listener.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and exit without connecting to Discord.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.load()
    if args.check:
        print(f"token_configured={bool(settings.token)}")
        print(f"home_channel_configured={bool(settings.home_channel_id)}")
        print(f"allowed_users_count={len(settings.allowed_users)}")
        print(f"allow_all_users={settings.allow_all_users}")
        print(f"mayor_home={settings.mayor_home}")
        print(f"db_path={settings.db_path}")
        print(f"inbox_dir={settings.inbox_dir}")
        return

    if not settings.allow_all_users and not settings.allowed_users:
        raise SystemExit(
            "No Discord allowlist configured. Set MAYOR_DISCORD_ALLOWED_USERS "
            "or approve a user in Hermes pairing state."
        )
    gateway = MayorDiscordGateway(settings)
    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
