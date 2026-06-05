from __future__ import annotations

import asyncio
import argparse
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

import discord
from discord.ext import commands

from bemine.capabilities import build_capability_manifest, render_capability_manifest
from bemine.config import (
    BemineSettings,
    config_path,
    load_bemine_config,
    render_toml_config,
    update_bemine_config,
)
from bemine.credentials import CredentialInventory, PermissionGate
from bemine.kimi_handler import KimiHandler, render_kimi_status
from bemine.kernel import BemineKernel
from bemine.status import collect_fleet_status, render_status

from .config import Settings
from .dispatch import (
    DispatchRequest,
    DispatchProgress,
    DispatchResult,
    ProjectDispatcher,
    ProjectRoute,
    dispatch_status_payload,
    infer_project_route,
    render_dispatch_status,
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

@dataclass(frozen=True)
class LocalToolResult:
    tool: str
    text: str


@dataclass(frozen=True)
class LocalToolSpec:
    name: str
    description: str
    arguments: str
    handler: Callable[[Mapping[str, Any]], str]


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


def _config_text(*, toml: bool = False) -> str:
    data = load_bemine_config()
    if toml:
        return render_toml_config(data)
    settings = BemineSettings.load()
    dispatch = dispatch_status_payload(settings)
    lines = [
        "Bemine config:",
        f"- file: {config_path()}",
        f"- device: {settings.device_id}",
        f"- state: {settings.state_dir}",
        f"- vault: {settings.vault_dir}",
        f"- status devices: {', '.join(settings.status_devices)}",
        "",
        render_dispatch_status(dispatch).strip(),
    ]
    return "\n".join(lines) + "\n"


def _local_tool_response(
    content: str,
    *,
    router: Callable[[str, str], Mapping[str, Any] | None] | None = None,
) -> LocalToolResult | None:
    decision = _route_local_tool(content, router=router)
    if decision is None:
        return None
    tool = _local_tool_by_name().get(str(decision.get("tool") or ""))
    if tool is None:
        return None
    args = decision.get("arguments")
    arguments = args if isinstance(args, Mapping) else {}
    return LocalToolResult(tool=tool.name, text=tool.handler(arguments))


def _route_local_tool(
    content: str,
    *,
    router: Callable[[str, str], Mapping[str, Any] | None] | None = None,
) -> Mapping[str, Any] | None:
    normalized = " ".join(content.strip().split())
    if not normalized:
        return None
    catalog = _tool_catalog_text()
    router = router or _route_local_tool_with_kimi
    try:
        decision = router(normalized, catalog)
    except Exception as exc:
        LOG.debug("local tool router failed: %s", exc)
        return None
    if not isinstance(decision, Mapping):
        return None
    tool_name = str(decision.get("tool") or "")
    if tool_name in {"", "project.dispatch", "none"}:
        return None
    if tool_name not in _local_tool_by_name():
        return None
    confidence = _float(decision.get("confidence"), default=1.0)
    return decision if confidence >= 0.55 else None


def _route_local_tool_with_kimi(content: str, catalog: str) -> Mapping[str, Any] | None:
    settings = BemineSettings.load()
    prompt = (
        "Discord operator message:\n"
        f"{content}\n\n"
        "Available local Bemine tools:\n"
        f"{catalog}\n\n"
        "Choose one local tool only when the user is asking Bemine to inspect or "
        "change harness state. Choose project.dispatch for open-ended project work, "
        "conversation, coding tasks, or anything that does not clearly need a local "
        "Bemine tool. Return only JSON with keys: tool, arguments, confidence."
    )
    result = KimiHandler(settings.hermes_home).ask(
        prompt,
        system=(
            "You are Bemine's Discord tool router. Output strict JSON only. "
            "Do not explain commands to the user."
        ),
        max_tokens=300,
        timeout=20.0,
    )
    return _parse_tool_decision(result.text)


def _parse_tool_decision(text: str) -> Mapping[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _tool_catalog_text() -> str:
    return "\n".join(
        f"- {tool.name}: {tool.description} Arguments: {tool.arguments}"
        for tool in _local_tools()
    )


def _local_tool_by_name() -> dict[str, LocalToolSpec]:
    return {tool.name: tool for tool in _local_tools()}


def _local_tools() -> tuple[LocalToolSpec, ...]:
    return (
        LocalToolSpec(
            name="config.show",
            description="Show Bemine's active config file and effective runtime settings.",
            arguments='{"format": "summary|toml"}',
            handler=_tool_config_show,
        ),
        LocalToolSpec(
            name="config.update_dispatch",
            description="Change the default Discord dispatch runtime/model in bemine.toml.",
            arguments='{"runtime": "codex|kimi", "model": "model id such as gpt-5.4-mini or kimi-k2.6"}',
            handler=_tool_config_update_dispatch,
        ),
        LocalToolSpec(
            name="dispatch.status",
            description="Show the active Discord dispatch runtime, model, provider, and tool-call support.",
            arguments="{}",
            handler=_tool_dispatch_status,
        ),
        LocalToolSpec(
            name="status.show",
            description="Show recent Codex, Claude, and Kimi sessions by last interaction.",
            arguments='{"limit": 1-20}',
            handler=_tool_status_show,
        ),
        LocalToolSpec(
            name="rundown.show",
            description="Show the project rundown from indexed history and vault memory.",
            arguments="{}",
            handler=lambda _args: _build_rundown(),
        ),
        LocalToolSpec(
            name="capabilities.show",
            description="Show the redacted credential and infrastructure capability manifest.",
            arguments="{}",
            handler=lambda _args: _capability_text(),
        ),
        LocalToolSpec(
            name="permissions.pending",
            description="Show pending gated credential permission requests.",
            arguments="{}",
            handler=lambda _args: _pending_permissions_text(),
        ),
    )


def _tool_config_show(args: Mapping[str, Any]) -> str:
    return _config_text(toml=str(args.get("format") or "").lower() == "toml")


def _tool_config_update_dispatch(args: Mapping[str, Any]) -> str:
    model = _normalize_model_id(str(args.get("model") or ""))
    runtime = str(args.get("runtime") or "").strip().lower()
    if not runtime:
        runtime = _runtime_for_model(model)
    if runtime not in {"codex", "kimi"}:
        raise ValueError("Discord dispatch currently supports codex and kimi runtimes")
    updates: dict[tuple[str, str], object] = {("dispatch", "runtime"): runtime}
    if model:
        updates[("kimi" if runtime == "kimi" else "codex", "model")] = model
    path = update_bemine_config(updates)
    dispatch = dispatch_status_payload(BemineSettings.load())
    return (
        f"Updated `{path}`.\n"
        f"- runtime: {dispatch.get('runtime')}\n"
        f"- model: {dispatch.get('model')}\n"
        f"- source: {dispatch.get('model_source')}"
    )


def _tool_dispatch_status(_args: Mapping[str, Any]) -> str:
    return render_dispatch_status(dispatch_status_payload(BemineSettings.load()))


def _tool_status_show(args: Mapping[str, Any]) -> str:
    limit = max(1, min(20, int(_float(args.get("limit"), default=8))))
    settings = BemineSettings.load()
    statuses, errors = collect_fleet_status(settings)
    return render_status(statuses, errors, limit=limit)


def _runtime_for_model(model: str) -> str:
    lowered = model.lower()
    if "kimi" in lowered or lowered.startswith("k2") or "k2." in lowered:
        return "kimi"
    if "gpt" in lowered or "codex" in lowered or "openai" in lowered:
        return "codex"
    return resolve_dispatch_runtime()


def _normalize_model_id(value: str) -> str:
    value = re.sub(r"\bplease\b", "", value, flags=re.IGNORECASE)
    value = value.strip(" .,!?:;`\"'")
    return "-".join(value.replace("_", "-").split()).lower()


def _float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
            settings = BemineSettings.load()
            statuses, errors = await asyncio.to_thread(
                collect_fleet_status,
                settings,
            )
            text = render_status(statuses, errors, limit=8)
            await message.reply(
                _discord_chunk(text, limit=1900)[0], mention_author=False
            )
            return
        if normalized_lower in {
            "!mayor rundown",
            "mayor rundown",
            "!bemine rundown",
            "bemine rundown",
        }:
            rundown = await asyncio.to_thread(_build_rundown)
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
            lines = await asyncio.to_thread(_credential_lines)
            await message.reply(
                _discord_chunk("Credential profiles:\n" + "\n".join(lines), limit=1900)[
                    0
                ],
                mention_author=False,
            )
            return
        if normalized_lower in {
            "!mayor capabilities",
            "mayor capabilities",
            "!bemine capabilities",
            "bemine capabilities",
        }:
            text = await asyncio.to_thread(_capability_text)
            await message.reply(
                _discord_chunk(text, limit=1900)[0], mention_author=False
            )
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
            await message.reply(
                _discord_chunk(render_kimi_status(config), limit=1900)[0],
                mention_author=False,
            )
            return
        if normalized_lower in {
            "!mayor permissions",
            "mayor permissions",
            "!bemine permissions",
            "bemine permissions",
        }:
            text = await asyncio.to_thread(_pending_permissions_text)
            await message.reply(
                _discord_chunk(text, limit=1900)[0], mention_author=False
            )
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

        if normalized:
            try:
                local_result = await asyncio.to_thread(
                    _local_tool_response,
                    normalized,
                )
            except Exception as exc:
                await _acknowledge_message(message, emoji=WARNING_REACTION)
                await message.reply(
                    f"`tool: local`\nLocal tool failed: {exc}",
                    mention_author=False,
                )
                return
            if local_result:
                await _acknowledge_message(message, emoji=SUCCESS_REACTION)
                for chunk in _discord_chunk(
                    f"`tool: {local_result.tool}`\n{local_result.text}",
                    limit=1900,
                ):
                    await message.reply(chunk, mention_author=False)
                return

        settings = BemineSettings.load()
        route = _project_route_for_message(message, settings=settings)
        if route and (normalized or saved_attachments):
            await _acknowledge_message(message, emoji=WORKING_REACTION)
            request = DispatchRequest(
                channel_id=str(message.channel.id),
                channel_name=_message_channel_name(message),
                message_id=str(message.id),
                author_name=getattr(message.author, "global_name", None)
                or str(message.author),
                content=normalized,
                attachments=saved_attachments,
                route_reason=route.reason,
            )
            loop = asyncio.get_running_loop()

            def publish_progress(progress: DispatchProgress) -> None:
                future = asyncio.run_coroutine_threadsafe(
                    _send_dispatch_progress(message, progress),
                    loop,
                )
                try:
                    future.result(timeout=5)
                except Exception as exc:
                    LOG.debug("could not send dispatch progress: %s", exc)

            result = await asyncio.to_thread(
                ProjectDispatcher(settings).dispatch,
                route.project,
                request,
                publish_progress,
            )
            await _acknowledge_message(
                message,
                emoji=SUCCESS_REACTION if result.ok else WARNING_REACTION,
            )
            for chunk in _discord_chunk(
                _dispatch_response_text(result, route), limit=1900
            ):
                await message.reply(chunk, mention_author=False)
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
                "I could not infer a route for this attachment-only message."
            )
        return "Received by Mayor."

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
    message: discord.Message,
    progress: DispatchProgress,
) -> None:
    text = _dispatch_progress_text(progress)
    if not text:
        return
    try:
        await message.reply(text, mention_author=False)
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
