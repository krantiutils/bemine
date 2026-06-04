from __future__ import annotations

import asyncio
import argparse
import logging
import re
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

from bemine.capabilities import build_capability_manifest, render_capability_manifest
from bemine.config import BemineSettings
from bemine.credentials import CredentialInventory, PermissionGate
from bemine.kernel import BemineKernel
from bemine.status import collect_fleet_status, render_status

from .config import Settings
from .store import Store


LOG = logging.getLogger("mayor.discord.gateway")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
VOICE_SUFFIXES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm"}
APPROVAL_CODE_RE = re.compile(
    r"\b((?:!?(?:mayor|bemine)\s+)?approve\s+\S+\s+)(\d{6,8})\b",
    re.IGNORECASE,
)


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
            normalized = message.content.strip()
            normalized_lower = normalized.lower()

            if normalized_lower in {"!mayor ping", "mayor ping"}:
                await message.reply("Mayor gateway online.", mention_author=False)
                return
            if normalized_lower in {
                "status",
                "!mayor status",
                "mayor status",
                "!bemine status",
                "bemine status",
            }:
                async with message.channel.typing():
                    settings = BemineSettings.load()
                    statuses, errors = await asyncio.to_thread(
                        collect_fleet_status,
                        settings,
                    )
                    text = render_status(statuses, errors, limit=8)
                await message.reply(_discord_chunk(text, limit=1900)[0], mention_author=False)
                return
            if normalized_lower in {
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
            if normalized_lower in {
                "!mayor credentials",
                "mayor credentials",
                "!bemine credentials",
                "bemine credentials",
            }:
                kernel = BemineKernel(BemineSettings.load())
                CredentialInventory(kernel.db, device_id=kernel.settings.device_id).scan()
                rows = kernel.db.list_credential_profiles()
                lines = [
                    f"{row['credential_id']} [{row['kind']}] {row['approval_policy']}"
                    + (" totp" if row["totp_required"] else "")
                    for row in rows
                ]
                await message.reply(
                    _discord_chunk("Credential profiles:\n" + "\n".join(lines), limit=1900)[0],
                    mention_author=False,
                )
                return
            if normalized_lower in {
                "!mayor capabilities",
                "mayor capabilities",
                "!bemine capabilities",
                "bemine capabilities",
            }:
                kernel = BemineKernel(BemineSettings.load())
                CredentialInventory(kernel.db, device_id=kernel.settings.device_id).scan()
                rows = [dict(row) for row in kernel.db.list_credential_profiles()]
                manifest = build_capability_manifest(rows, device_id=kernel.settings.device_id)
                text = render_capability_manifest(manifest)
                await message.reply(_discord_chunk(text, limit=1900)[0], mention_author=False)
                return
            if normalized_lower in {
                "!mayor permissions",
                "mayor permissions",
                "!bemine permissions",
                "bemine permissions",
            }:
                kernel = BemineKernel(BemineSettings.load())
                rows = kernel.db.list_permission_requests(status="pending")
                if not rows:
                    text = "No pending permission requests."
                else:
                    text = "Pending permission requests:\n" + "\n".join(
                        f"{row['request_id']} {row['credential_id']} - {row['reason']}"
                        for row in rows
                    )
                await message.reply(_discord_chunk(text, limit=1900)[0], mention_author=False)
                return
            approval = _parse_approval_command(normalized)
            if approval:
                request_id, totp_code = approval
                kernel = BemineKernel(BemineSettings.load())
                try:
                    PermissionGate(kernel.db).approve(
                        request_id,
                        actor=f"discord:{message.author.id}",
                        totp_code=totp_code,
                    )
                except Exception as exc:
                    await message.reply(f"Approval failed: {exc}", mention_author=False)
                    return
                await message.reply(f"Approved `{request_id}`.", mention_author=False)
                return
            denial = _parse_deny_command(normalized)
            if denial:
                kernel = BemineKernel(BemineSettings.load())
                try:
                    PermissionGate(kernel.db).deny(
                        denial,
                        actor=f"discord:{message.author.id}",
                    )
                except Exception as exc:
                    await message.reply(f"Deny failed: {exc}", mention_author=False)
                    return
                await message.reply(f"Denied `{denial}`.", mention_author=False)
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
            content=_redact_sensitive_message(message.content or ""),
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


def _redact_sensitive_message(content: str) -> str:
    return APPROVAL_CODE_RE.sub(lambda m: f"{m.group(1)}<totp-redacted>", content)


def _parse_approval_command(content: str) -> tuple[str, str] | None:
    parts = content.strip().split()
    if not parts:
        return None
    if parts[0].lower() in {"!mayor", "mayor", "!bemine", "bemine"}:
        parts = parts[1:]
    if len(parts) < 2 or parts[0].lower() != "approve":
        return None
    request_id = parts[1]
    totp_code = parts[2] if len(parts) >= 3 else ""
    return request_id, totp_code


def _parse_deny_command(content: str) -> str | None:
    parts = content.strip().split()
    if not parts:
        return None
    if parts[0].lower() in {"!mayor", "mayor", "!bemine", "bemine"}:
        parts = parts[1:]
    if len(parts) < 2 or parts[0].lower() not in {"deny", "reject"}:
        return None
    return parts[1]


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
