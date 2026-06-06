from __future__ import annotations

import asyncio
import argparse
import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import discord
from discord.ext import commands

from bemine.capabilities import build_capability_manifest, render_capability_manifest
from bemine.config import BemineSettings, config_int
from bemine.credentials import CredentialInventory, PermissionGate
from bemine.kimi_handler import KimiHandler, render_kimi_status
from bemine.kernel import BemineKernel
from bemine.status import _redact, collect_fleet_status, render_status

from .config import Settings
from .dispatch import (
    DispatchProgress,
    DispatchResult,
    ProjectRoute,
    infer_project_route,
    resolve_dispatch_runtime,
)
from .store import Store


LOG = logging.getLogger("mayor.discord.gateway")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
VOICE_SUFFIXES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm"}
ACK_REACTION = "👀"
WORKING_REACTION = "🛠️"
SUCCESS_REACTION = "✅"
WARNING_REACTION = "⚠️"
APPROVAL_CODE_RE = re.compile(
    r"\b((?:!?(?:mayor|bemine)\s+)?approve\s+\S+\s+)(\d{6,8})\b",
    re.IGNORECASE,
)

class DiscordResponder:
    def __init__(self, store: Store, source_message: discord.Message) -> None:
        self.store = store
        self.source_message = source_message
        self.used_reply = False

    async def send(self, content: str) -> discord.Message:
        if not self.used_reply:
            sent = await self.source_message.reply(content, mention_author=False)
            self.used_reply = True
        else:
            sent = await self.source_message.channel.send(content)
        self.store.record_outbound(
            discord_message_id=str(getattr(sent, "id", "")) or None,
            channel_id=str(self.source_message.channel.id),
            content=content,
            files=[],
        )
        return sent

    async def send_chunks(self, text: str, *, limit: int = 1900) -> None:
        for chunk in _discord_chunk(text, limit=limit):
            await self.send(chunk)


def _discord_context_message_limit() -> int:
    return max(0, min(50, config_int("discord", "context_messages", 12)))


def _format_conversation_context(
    rows: list[Mapping[str, Any]], *, limit: int = 6000
) -> str:
    lines: list[str] = []
    for row in rows:
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            content = "(attachment or empty message)"
        author = _redact(str(row.get("author_name", "unknown")))
        role = str(row.get("role") or "user")
        if role == "assistant":
            author = f"{author} (bot)"
        created_at = str(row.get("created_at", ""))
        lines.append(f"- {author} [{created_at}]: {_redact(content)}")
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[-limit:].lstrip()


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


def _build_rundown() -> str:
    kernel = BemineKernel(BemineSettings.load())
    kernel.scan_local()
    return kernel.project_rundown(limit=12)


def _credential_lines() -> list[str]:
    kernel = BemineKernel(BemineSettings.load())
    CredentialInventory(kernel.db, device_id=kernel.settings.device_id).scan()
    rows = kernel.db.list_credential_profiles()
    return [
        f"{row['credential_id']} [{row['kind']}] {row['approval_policy']}"
        + (" totp" if row["totp_required"] else "")
        for row in rows
    ]


def _capability_text() -> str:
    kernel = BemineKernel(BemineSettings.load())
    CredentialInventory(kernel.db, device_id=kernel.settings.device_id).scan()
    rows = [dict(row) for row in kernel.db.list_credential_profiles()]
    manifest = build_capability_manifest(rows, device_id=kernel.settings.device_id)
    return render_capability_manifest(manifest)


def _pending_permissions_text() -> str:
    kernel = BemineKernel(BemineSettings.load())
    rows = kernel.db.list_permission_requests(status="pending")
    if not rows:
        return "No pending permission requests."
    return "Pending permission requests:\n" + "\n".join(
        f"{row['request_id']} {row['credential_id']} - {row['reason']}" for row in rows
    )


async def _acknowledge_message(
    message: discord.Message, *, emoji: str = ACK_REACTION
) -> bool:
    try:
        await message.add_reaction(emoji)
    except (discord.DiscordException, AttributeError, TypeError) as exc:
        LOG.debug("could not add Discord acknowledgement reaction: %s", exc)
        return False
    return True


@asynccontextmanager
async def _best_effort_typing(channel: discord.abc.Messageable) -> AsyncIterator[None]:
    try:
        typing = channel.typing()
        await typing.__aenter__()
    except (discord.DiscordException, AttributeError, TypeError) as exc:
        LOG.debug("could not start Discord typing indicator: %s", exc)
        yield
        return
    try:
        yield
    finally:
        try:
            await typing.__aexit__(None, None, None)
        except (discord.DiscordException, AttributeError, TypeError) as exc:
            LOG.debug("could not stop Discord typing indicator: %s", exc)


def run_bound_agent(
    settings,
    message,
    normalized,
    saved_attachments=None,
    progress_callback=None,
):
    """Deterministic: resolve the session bound to this channel/thread, append the user
    message, run Bemine's agent loop. No intent classification."""
    from bemine.db import BemineDB
    from bemine.agent.session import ensure_session
    from bemine.agent.loop import AgentLoop
    from bemine.agent.clients import build_client_for_runtime
    from mayor_discord.dispatch import resolve_channel_binding

    db = BemineDB(settings.db_path)
    binding = resolve_channel_binding(settings, message)
    channel = message.channel
    is_dm = getattr(message, "guild", None) is None
    chan_type = str(getattr(channel, "type", "")).lower()
    chat_type = "dm" if is_dm else ("thread" if "thread" in chan_type else "channel")
    channel_id = str(getattr(channel, "id", ""))
    session_id = ensure_session(
        db,
        settings,
        channel_id=channel_id,
        chat_type=chat_type,
        chat_id=channel_id,
        thread_id=channel_id,
        project_id=binding.project_id,
        runtime=binding.runtime,
        model=binding.model or binding.runtime,
        cwd=binding.cwd,
    )
    client = build_client_for_runtime(settings, binding.runtime)
    loop = AgentLoop(settings=settings, db=db, client=client)
    result = loop.run(
        session_id=session_id,
        project_id=binding.project_id,
        cwd=binding.cwd,
        user_message=normalized,
        system=binding.system,
        progress_callback=progress_callback,
    )
    return result.final_text


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
        self._dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._dispatch_cancel_events: dict[str, threading.Event] = {}
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
            if message.type not in {
                discord.MessageType.default,
                discord.MessageType.reply,
            }:
                return
            if not self._is_allowed(message.author):
                LOG.warning("ignored unauthorized Discord user %s", message.author.id)
                return

            await _acknowledge_message(message)
            async with _best_effort_typing(message.channel):
                await self._handle_allowed_message(message)

    async def _handle_allowed_message(self, message: discord.Message) -> None:
        saved_attachments = await self._record_message(message)
        normalized = message.content.strip()
        normalized_lower = normalized.lower()
        responder = DiscordResponder(self.store, message)

        if normalized_lower in {"!mayor ping", "mayor ping"}:
            await responder.send("Mayor gateway online.")
            return
        if normalized_lower in {
            "status",
            "!mayor status",
            "mayor status",
            "!bemine status",
            "bemine status",
        }:
            settings = BemineSettings.load()
            statuses, errors = await asyncio.to_thread(
                collect_fleet_status,
                settings,
            )
            text = render_status(statuses, errors, limit=8)
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        if normalized_lower in {
            "!mayor rundown",
            "mayor rundown",
            "!bemine rundown",
            "bemine rundown",
        }:
            rundown = await asyncio.to_thread(_build_rundown)
            await responder.send(_discord_chunk(rundown, limit=1900)[0])
            return
        if normalized_lower in {
            "!mayor credentials",
            "mayor credentials",
            "!bemine credentials",
            "bemine credentials",
        }:
            lines = await asyncio.to_thread(_credential_lines)
            await responder.send(
                _discord_chunk("Credential profiles:\n" + "\n".join(lines), limit=1900)[
                    0
                ]
            )
            return
        if normalized_lower in {
            "!mayor capabilities",
            "mayor capabilities",
            "!bemine capabilities",
            "bemine capabilities",
        }:
            text = await asyncio.to_thread(_capability_text)
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        if normalized_lower in {
            "!mayor kimi",
            "mayor kimi",
            "!mayor kimi status",
            "mayor kimi status",
            "!bemine kimi",
            "bemine kimi",
            "!bemine kimi status",
            "bemine kimi status",
            "kimi status",
        }:
            config = await asyncio.to_thread(
                lambda: KimiHandler(BemineSettings.load().hermes_home).status()
            )
            await responder.send(
                _discord_chunk(render_kimi_status(config), limit=1900)[0]
            )
            return
        if normalized_lower in {
            "!mayor permissions",
            "mayor permissions",
            "!bemine permissions",
            "bemine permissions",
        }:
            text = await asyncio.to_thread(_pending_permissions_text)
            await responder.send(_discord_chunk(text, limit=1900)[0])
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
                await responder.send(f"Approval failed: {exc}")
                return
            await responder.send(f"Approved `{request_id}`.")
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
                await responder.send(f"Deny failed: {exc}")
                return
            await responder.send(f"Denied `{denial}`.")
            return

        if normalized or saved_attachments:
            await _acknowledge_message(message, emoji=WORKING_REACTION)
            try:
                final_text = await asyncio.to_thread(
                    run_bound_agent,
                    BemineSettings.load(),
                    message,
                    normalized,
                    saved_attachments,
                )
            except Exception as exc:
                await _acknowledge_message(message, emoji=WARNING_REACTION)
                await responder.send(f"Dispatch failed: {exc}")
                return
            await _acknowledge_message(message, emoji=SUCCESS_REACTION)
            await responder.send_chunks(final_text or "(no output)", limit=1900)
            return

        await self.bot.process_commands(message)

    def _is_allowed(self, author: discord.abc.User) -> bool:
        if self.settings.allow_all_users:
            return True
        return str(author.id) in self.settings.allowed_users

    def _default_reply(self, saved_attachments: list[Path]) -> str:
        if saved_attachments:
            return (
                "Received by Mayor. "
                f"Saved {len(saved_attachments)} attachment(s). "
                "I could not infer a route for this attachment-only message."
            )
        return "Received by Mayor."

    def _conversation_context(self, message: discord.Message) -> str:
        rows = self.store.recent_channel_messages(
            channel_id=str(message.channel.id),
            before_message_id=str(message.id),
            limit=_discord_context_message_limit(),
        )
        return _format_conversation_context(rows)

    async def _record_message(self, message: discord.Message) -> list[Path]:
        message_id = str(message.id)
        channel_id = str(message.channel.id)
        guild_id = str(message.guild.id) if message.guild else None
        is_dm = isinstance(message.channel, discord.DMChannel) or message.guild is None
        author_name = getattr(message.author, "global_name", None) or str(
            message.author
        )
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


def _project_route_for_message(
    message: discord.Message,
    *,
    settings: BemineSettings,
) -> ProjectRoute | None:
    return infer_project_route(
        channel_name=_message_channel_name(message),
        category_name=_message_category_name(message),
        content=str(message.content or ""),
        state_dir=settings.state_dir,
        repo_dir=Path.cwd(),
        mentioned_channels=_mentioned_channels(message),
        allow_default=True,
    )


def _message_channel_name(message: discord.Message) -> str:
    return _channel_name(message.channel)


def _message_category_name(message: discord.Message) -> str:
    return _channel_category_name(message.channel)


def _mentioned_channels(message: discord.Message) -> list[tuple[str, str]]:
    return [
        (_channel_name(channel), _channel_category_name(channel))
        for channel in getattr(message, "channel_mentions", [])
    ]


def _channel_name(channel: object) -> str:
    return str(getattr(channel, "name", "") or "")


def _channel_category_name(channel: object) -> str:
    category = getattr(channel, "category", None)
    return str(getattr(category, "name", "") or "")


def _discord_chunk(text: str, *, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    open_fence = ""
    for line in text.splitlines():
        line_len = len(line) + 1
        close_len = len(open_fence) + 1 if open_fence else 0
        if current and current_len + line_len + close_len > limit:
            if open_fence:
                current.append(open_fence)
            chunks.append("\n".join(current))
            current = [open_fence] if open_fence else []
            current_len = len(open_fence) + 1 if open_fence else 0
        current.append(line)
        current_len += line_len
        fence = _markdown_fence_marker(line)
        if fence:
            open_fence = "" if open_fence else fence
    if current:
        if open_fence:
            current.append(open_fence)
        chunks.append("\n".join(current))
    return chunks or [text[:limit]]


def _markdown_fence_marker(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("```"):
        return "```"
    if stripped.startswith("~~~"):
        return "~~~"
    return ""


def _dispatch_response_text(result: DispatchResult, route: ProjectRoute) -> str:
    if result.ok:
        return result.output
    runtime = resolve_dispatch_runtime(route.project.runtime).capitalize()
    return f"{runtime} dispatch issue for `#{route.project.channel_name}`:\n{result.output}"


async def _send_dispatch_progress(
    responder: DiscordResponder,
    progress: DispatchProgress,
) -> None:
    text = _dispatch_progress_text(progress)
    if not text:
        return
    try:
        await responder.send(text)
    except discord.DiscordException as exc:
        LOG.debug("could not reply with dispatch progress: %s", exc)


def _dispatch_progress_text(progress: DispatchProgress) -> str:
    return progress.summary.strip()


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
