from __future__ import annotations

import asyncio
import argparse
import logging
import re
import shlex
import threading
from dataclasses import dataclass
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import discord
from discord.ext import commands

from bemine.capabilities import build_capability_manifest, render_capability_manifest
from bemine.backlog import add_backlog_item, default_backlog_owner
from bemine.commands import run_command
from bemine.config import BemineSettings, config_int, config_string
from bemine.security.credentials import CredentialInventory, PermissionGate
from bemine.goals import render_goal_start, start_goal
from bemine.kimi_handler import KimiHandler, render_kimi_status
from bemine.kernel import BemineKernel
from bemine.index.status import _redact, collect_fleet_status, render_status
from bemine.deploy.heartbeat import write_heartbeat, current_head_sha

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
GOAL_PREFIX_RE = re.compile(
    r"^\s*(?:!?(?:mayor|bemine)\s+)?(?:(?:start|run|begin|launch)\s+(?:a\s+)?)?goal\s+",
    re.IGNORECASE,
)
BACKLOG_RE = re.compile(
    r"^\s*(?:!?(?:mayor|bemine)\s+)?(?:(?P<owner1>pa|smm)\s+"
    r"(?P<kind1>idea|task|backlog)|(?:store|save|remember)\s+(?:this\s+)?"
    r"(?:(?P<kind2>idea|task|backlog)\s+)?for\s+(?P<owner2>pa|smm))\s*[:\-]\s*"
    r"(?P<text>.+)\s*$|^\s*(?:!?(?:mayor|bemine)\s+)?(?:store|save|remember)\s+(?:this\s+)?"
    r"(?:(?P<kind3>idea|task|backlog)\s+)?for\s+(?P<owner3>pa|smm)\s+(?P<text2>.+)\s*$",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Slash command bridge — deterministic ops, no LLM in the path.
# ---------------------------------------------------------------------------

# Maps Discord slash command name -> registry command name.
_SLASH_TO_COMMAND: dict[str, str] = {
    "status": "status",
    "rundown": "rundown",
    "jobs": "jobs.list",
    "config": "config.show",
    "smm": "smm.draft",
    "subagents": "subagents.list",
    "phantom": "phantom.status",
    "health": "health",
    "continuity": "continuity",
    "help": "help",
    "routes": "discord.routes",
    "dispatch": "dispatch.status",
    "pa": "pa.plan",
    "goals": "goals.list",
    "dream": "dream.run",
    "brief": "brief.run",
    "usage": "usage",
    "capabilities": "capabilities",
}

_GROUPED_SLASH_ROOTS = {"subagents", "phantom"}


def slash_command_text(settings: BemineSettings, slash_name: str, args: dict) -> str:
    """Resolve a slash command name to a registry command and run it.

    Returns the plain-text result suitable for sending back to Discord.
    No LLM is invoked; this is entirely deterministic.
    """
    if slash_name in {"subagents", "subagents.list"}:
        command = "subagents.list"
    elif slash_name == "subagents.show":
        command = "subagents.show"
    elif slash_name in {"phantom", "phantom.status"}:
        command = "phantom.status"
    elif slash_name == "phantom.build":
        command = "phantom.build"
    elif slash_name == "phantom.install":
        command = "phantom.install"
    elif slash_name == "goal":
        command = "goal.start"
    elif slash_name == "backlog":
        command = "backlog.list"
    elif slash_name == "discord.routes":
        command = "discord.routes"
    elif slash_name in {"discord.sync-plan", "discord.sync_plan"}:
        command = "discord.sync_plan"
    else:
        command = _SLASH_TO_COMMAND.get(slash_name)
    if not command:
        return f"Unknown command: {slash_name}"
    return run_command(command, settings, args).text


def register_slash_commands(
    tree: discord.app_commands.CommandTree,
    *,
    settings_loader,
    is_allowed_check=None,
) -> None:
    """Register each slash command in _SLASH_TO_COMMAND on *tree*.

    ``settings_loader`` is a zero-argument callable returning a
    ``BemineSettings`` instance (allows dependency injection in tests).
    ``is_allowed_check`` is an optional ``(discord.Interaction) -> bool``
    predicate; when provided, commands silently return an "Access denied"
    message for unauthorised users.
    """

    async def _run_slash(interaction: discord.Interaction, slash_name: str) -> None:
        if is_allowed_check is not None and not is_allowed_check(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        settings = await asyncio.to_thread(settings_loader)
        text = await asyncio.to_thread(slash_command_text, settings, slash_name, {})
        chunks = _discord_chunk(text, limit=1900)
        await interaction.response.send_message(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    for slash_name in _SLASH_TO_COMMAND:
        if slash_name in _GROUPED_SLASH_ROOTS:
            continue

        # Capture slash_name in a default argument to avoid closure-over-loop issues.
        async def _handler(
            interaction: discord.Interaction,
            _name: str = slash_name,
        ) -> None:
            await _run_slash(interaction, _name)

        _handler.__name__ = slash_name
        cmd = discord.app_commands.Command(
            name=slash_name,
            description=f"Bemine: {slash_name}",
            callback=_handler,
        )
        tree.add_command(cmd)

    async def _send_slash_result(
        interaction: discord.Interaction,
        *,
        command_name: str,
        args: dict[str, Any] | None = None,
    ) -> None:
        if is_allowed_check is not None and not is_allowed_check(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        settings = await asyncio.to_thread(settings_loader)
        text = await asyncio.to_thread(
            run_command,
            command_name,
            settings,
            args or {},
        )
        chunks = _discord_chunk(text.text, limit=1900)
        await interaction.response.send_message(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    async def _backlog_handler(
        interaction: discord.Interaction,
        owner: str = "",
        limit: int = 20,
    ) -> None:
        await _send_slash_result(
            interaction,
            command_name="backlog.list",
            args={"owner": owner, "limit": limit},
        )

    _backlog_handler.__name__ = "backlog"
    backlog_cmd = discord.app_commands.Command(
        name="backlog",
        description="Bemine: inspect the PA/SMM backlog",
        callback=_backlog_handler,
    )
    tree.add_command(backlog_cmd)

    async def _goal_handler(
        interaction: discord.Interaction,
        project: str,
        brief: str,
    ) -> None:
        if is_allowed_check is not None and not is_allowed_check(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        settings = await asyncio.to_thread(settings_loader)
        cwd = ""
        route = infer_project_route(
            channel_name=_channel_name(getattr(interaction, "channel", None)),
            category_name=_channel_category_name(getattr(interaction, "channel", None)),
            content="",
            state_dir=settings.state_dir,
            repo_dir=Path.cwd(),
            mentioned_channels=[],
            allow_default=False,
        )
        if route is not None:
            cwd = str(route.project.cwd)
        channel_id = str(getattr(getattr(interaction, "channel", None), "id", "") or "")
        text = await asyncio.to_thread(
            slash_command_text,
            settings,
            "goal",
            {"project": project, "brief": brief, "channel_id": channel_id, "cwd": cwd},
        )
        await interaction.response.send_message(text)

    _goal_handler.__name__ = "goal"
    goal_cmd = discord.app_commands.Command(
        name="goal",
        description="Bemine: start a managed goal session",
        callback=_goal_handler,
    )
    tree.add_command(goal_cmd)

    subagents_group = discord.app_commands.Group(
        name="subagents",
        description="Bemine: inspect configured subagent profiles",
    )

    @subagents_group.command(
        name="list", description="List configured subagent profiles"
    )
    async def subagents_list(interaction: discord.Interaction) -> None:
        await _send_slash_result(interaction, command_name="subagents.list")

    @subagents_group.command(
        name="show", description="Show one configured subagent profile"
    )
    async def subagents_show(
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await _send_slash_result(
            interaction, command_name="subagents.show", args={"name": name}
        )

    tree.add_command(subagents_group)

    phantom_group = discord.app_commands.Group(
        name="phantom",
        description="Bemine: inspect and manage Phantom",
    )

    @phantom_group.command(name="status", description="Inspect Phantom readiness")
    async def phantom_status(
        interaction: discord.Interaction,
        project_root: str = "",
        serial: str = "",
        java_home: str = "",
    ) -> None:
        await _send_slash_result(
            interaction,
            command_name="phantom.status",
            args={
                "project_root": project_root,
                "serial": serial,
                "java_home": java_home,
            },
        )

    @phantom_group.command(name="build", description="Build the Phantom APK")
    async def phantom_build(
        interaction: discord.Interaction,
        project_root: str = "",
        java_home: str = "",
        timeout: int = 600,
    ) -> None:
        await _send_slash_result(
            interaction,
            command_name="phantom.build",
            args={
                "project_root": project_root,
                "java_home": java_home,
                "timeout": timeout,
            },
        )

    @phantom_group.command(name="install", description="Install Phantom on the phone")
    async def phantom_install(
        interaction: discord.Interaction,
        project_root: str = "",
        serial: str = "",
        java_home: str = "",
        timeout: int = 600,
        build: bool = False,
        dry_run: bool = False,
    ) -> None:
        await _send_slash_result(
            interaction,
            command_name="phantom.install",
            args={
                "project_root": project_root,
                "serial": serial,
                "java_home": java_home,
                "timeout": timeout,
                "build": build,
                "dry_run": dry_run,
            },
        )

    tree.add_command(phantom_group)

    discord_group = discord.app_commands.Group(
        name="discord",
        description="Bemine: inspect Discord project routing",
    )

    @discord_group.command(name="routes", description="Inspect project channel routes")
    async def discord_routes(
        interaction: discord.Interaction,
        query: str = "",
        limit: int = 50,
    ) -> None:
        await _send_slash_result(
            interaction,
            command_name="discord.routes",
            args={"query": query, "limit": limit},
        )

    @discord_group.command(name="sync-plan", description="Dry-run project channel sync")
    async def discord_sync_plan(
        interaction: discord.Interaction,
        limit: int = 200,
        prune: bool = False,
    ) -> None:
        await _send_slash_result(
            interaction,
            command_name="discord.sync_plan",
            args={"limit": limit, "prune": prune},
        )

    tree.add_command(discord_group)


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


class _ProgressReporter:
    """Bridges the agent loop's sync progress_callback (called from a worker thread)
    to a single, live-edited Discord status message so the user sees it working."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, channel: discord.abc.Messageable
    ) -> None:
        self._loop = loop
        self._channel = channel
        self._lines: list[str] = []
        self._msg: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def __call__(self, text: str) -> None:
        # Sync, invoked from the agent worker thread.
        label = text.strip()
        if label.lower().startswith("tool:"):
            label = label[len("tool:") :].strip()
        label = label.replace("`", "'").replace("\n", " ")
        if len(label) > 180:
            label = label[:180] + "…"
        self._lines.append(f"• `{label}`")
        try:
            asyncio.run_coroutine_threadsafe(self._render(), self._loop)
        except RuntimeError:
            pass

    async def _render(self) -> None:
        async with self._lock:
            if self._closed:
                return
            body = "🛠️ Working…\n" + "\n".join(self._lines[-10:])
            try:
                if self._msg is None:
                    self._msg = await self._channel.send(body[:1900])
                else:
                    await self._msg.edit(content=body[:1900])
            except discord.DiscordException as exc:
                LOG.debug("progress render failed: %s", exc)

    async def finish(self, *, note: str | None = None) -> None:
        async with self._lock:
            self._closed = True
            if self._msg is None:
                return
            try:
                if note is not None:
                    await self._msg.edit(content=note[:1900])
                elif self._lines:
                    summary = "✅ Done — actions:\n" + "\n".join(self._lines[-10:])
                    await self._msg.edit(content=summary[:1900])
                else:
                    await self._msg.delete()
            except discord.DiscordException as exc:
                LOG.debug("progress finish failed: %s", exc)


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


def publish_gateway_heartbeat(settings=None, sha: str | None = None) -> None:
    """Write the gateway heartbeat to the canonical Bemine state dir (best-effort).

    The deploy pipeline reads heartbeats via ``BemineSettings.state_dir``, so the heartbeat
    MUST land there. The gateway's own ``self.settings`` is the Discord ``Settings`` dataclass,
    which has ``db_path``/``inbox_dir`` but no ``state_dir`` — passing it would raise. So we
    fall back to ``BemineSettings.load()`` whenever we're not handed something with a
    ``state_dir``.
    """
    try:
        if settings is None or not hasattr(settings, "state_dir"):
            settings = BemineSettings.load()
        write_heartbeat(settings, "gateway", sha or current_head_sha())
    except Exception:
        pass


def run_bound_agent(
    settings,
    message,
    normalized,
    saved_attachments=None,
    progress_callback=None,
    cancel_event=None,
):
    """Deterministic: resolve the session bound to this channel/thread, append the user
    message, run Bemine's agent loop (or codex app-server for codex runtime).
    No intent classification."""
    import json
    from bemine.db import BemineDB
    from bemine.agent.session import ensure_session
    from bemine.discord.dispatch import resolve_channel_binding

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

    if cancel_event is not None and cancel_event.is_set():
        return ""

    if binding.runtime == "codex":
        from bemine.agent.codex_appserver import run_codex_turn

        if progress_callback:
            progress_callback("codex (running…)")
        sandbox = config_string("codex", "sandbox", "workspace-write")
        codex_bin = config_string("codex", "bin", "") or "codex"

        # Persist user message to session transcript
        user_msg = {"role": "user", "content": normalized}
        db.append_bemine_session_message(
            session_id=session_id,
            role="user",
            content_json=json.dumps(user_msg),
        )

        resume_tid = db.get_codex_thread_id(session_id)
        final, new_tid = run_codex_turn(
            normalized,
            cwd=binding.cwd,
            developer_instructions=binding.system or "",
            model=None,  # let codex use its own ~/.codex/config.toml default
            sandbox=sandbox,
            codex_bin=codex_bin,
            resume_thread_id=resume_tid,
            progress_callback=progress_callback,
        )
        if new_tid and new_tid != resume_tid:
            db.set_codex_thread_id(session_id, new_tid)

        # Persist assistant message to session transcript
        assistant_msg = {"role": "assistant", "content": final}
        db.append_bemine_session_message(
            session_id=session_id,
            role="assistant",
            content_json=json.dumps(assistant_msg),
        )

        return final

    # Non-codex path: use AgentLoop with the appropriate LLM client
    from bemine.agent.loop import AgentLoop
    from bemine.agent.clients import build_client_for_runtime

    client = build_client_for_runtime(settings, binding.runtime)
    loop = AgentLoop(
        settings=settings,
        db=db,
        client=client,
        max_iterations=config_int("agent", "max_tool_iterations", 8),
    )
    result = loop.run(
        session_id=session_id,
        project_id=binding.project_id,
        cwd=binding.cwd,
        user_message=normalized,
        system=binding.system,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    return result.final_text


def maybe_consume_watcher_reply(settings, db, *, channel_id: str, text: str) -> bool:
    """If a watcher in this channel is awaiting a human reply, relay `text` into its
    session and resume the watcher. Returns True only if the reply was delivered (and
    thus consumed). On relay failure, pause the watcher and return False so the message
    falls through to normal handling."""
    if not text:
        return False
    watcher = db.get_project_watcher_by_channel(channel_id)
    if not watcher or not int(watcher.get("awaiting_reply", 0)):
        return False
    from bemine.jobs.project_watcher import relay_user_reply, default_deps

    delivered = relay_user_reply(settings, db, watcher, text, default_deps(settings))
    if not delivered:
        db.update_project_watcher(
            watcher["project_id"], awaiting_reply=0, status="paused"
        )
        return False
    return True


@dataclass(frozen=True)
class GoalCommand:
    project: str
    brief: str


@dataclass(frozen=True)
class BacklogCommand:
    owner: str
    text: str
    kind: str


def parse_goal_command(text: str) -> GoalCommand | None:
    match = GOAL_PREFIX_RE.match(text or "")
    if not match:
        return None
    body = text[match.end() :].strip()
    if not body:
        return None
    if ":" in body:
        project, brief = body.split(":", 1)
    else:
        try:
            parts = shlex.split(body)
        except ValueError:
            parts = body.split(None, 1)
        if len(parts) < 2:
            return None
        project = parts[0]
        brief = body[len(project) :]
    project = project.strip().strip("`#")
    brief = _strip_wrapping_quotes(brief.strip())
    if not project or not brief:
        return None
    return GoalCommand(project=project, brief=brief)


def parse_backlog_command(text: str) -> BacklogCommand | None:
    match = BACKLOG_RE.match(text or "")
    if not match:
        return None
    owner = (
        match.group("owner1") or match.group("owner2") or match.group("owner3") or ""
    ).upper()
    kind = (
        match.group("kind1") or match.group("kind2") or match.group("kind3") or "idea"
    ).lower()
    item_text = _strip_wrapping_quotes(
        (match.group("text") or match.group("text2") or "").strip()
    )
    if not owner or not item_text:
        return None
    return BacklogCommand(owner=owner, kind=kind, text=item_text)


def maybe_add_backlog_from_discord_message(
    settings: BemineSettings,
    db,
    message: discord.Message,
    text: str,
):
    command = parse_backlog_command(text)
    if command is None:
        return None
    route = infer_project_route(
        channel_name=_message_channel_name(message),
        category_name=_message_category_name(message),
        content=str(message.content or ""),
        state_dir=settings.state_dir,
        repo_dir=Path.cwd(),
        mentioned_channels=_mentioned_channels(message),
        allow_default=False,
    )
    project_id = route.project.project_id if route and route.project.project_id else ""
    return add_backlog_item(
        settings,
        owner=command.owner,
        text=f"{command.kind}: {command.text}",
        source=f"discord:{getattr(message.author, 'id', '')}",
        project=project_id,
        db=db,
    )


def maybe_start_goal_from_discord_message(
    settings: BemineSettings,
    db,
    message: discord.Message,
    text: str,
    *,
    conversation_context: str = "",
):
    command = parse_goal_command(text)
    if command is None:
        return None
    brief = _goal_brief_with_context(command.brief, conversation_context)
    return start_goal(
        settings,
        project=command.project,
        brief=brief,
        cwd=_goal_cwd_hint(settings, message, command.project),
        channel_id=str(message.channel.id),
        db=db,
    )


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _goal_brief_with_context(brief: str, conversation_context: str) -> str:
    context = conversation_context.strip()
    if not context:
        return brief
    if len(context) > 4000:
        context = context[-4000:].lstrip()
    return "\n\n".join(
        [
            brief,
            "Recent Discord context to consider before acting:",
            context,
        ]
    )


def _goal_cwd_hint(
    settings: BemineSettings,
    message: discord.Message,
    project: str,
) -> Path | None:
    route = infer_project_route(
        channel_name=_message_channel_name(message),
        category_name=_message_category_name(message),
        content=str(message.content or ""),
        state_dir=settings.state_dir,
        repo_dir=Path.cwd(),
        mentioned_channels=_mentioned_channels(message),
        allow_default=False,
    )
    if route is None:
        return None
    key = project.casefold()
    current_project_names = {
        "this",
        "here",
        "current",
        "project",
        route.project.channel_name.casefold(),
        (route.project.project_name or "").casefold(),
        (route.project.project_id or "").casefold(),
        route.project.cwd.name.casefold(),
    }
    if key in current_project_names:
        return route.project.cwd
    return None


class MayorDiscordGateway:
    def __init__(self, settings: Settings) -> None:
        settings.require_token()
        self.settings = settings
        self.store = Store(settings.db_path)
        try:
            self._boot_sha = current_head_sha()
        except Exception:
            self._boot_sha = ""

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
        register_slash_commands(
            self.bot.tree,
            settings_loader=BemineSettings.load,
            is_allowed_check=lambda interaction: self._is_allowed(interaction.user),
        )

        @self.bot.event
        async def on_ready() -> None:
            LOG.info("connected as %s", self.bot.user)
            LOG.info("state db: %s", self.settings.db_path)
            LOG.info("inbox: %s", self.settings.inbox_dir)
            publish_gateway_heartbeat(sha=self._boot_sha)
            asyncio.create_task(self._heartbeat_loop())
            try:
                synced = await self.bot.tree.sync()
                LOG.info("synced %d slash command(s) globally", len(synced))
            except discord.DiscordException as exc:
                LOG.warning("could not sync slash command tree: %s", exc)

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

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(publish_gateway_heartbeat, None, self._boot_sha)

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
        direct_command = _direct_observability_command(normalized_lower)
        if direct_command:
            text = await asyncio.to_thread(
                lambda: run_command(direct_command, BemineSettings.load(), {}).text
            )
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        subagents_args = _direct_subagents_command(normalized_lower)
        if subagents_args is not None:
            command_name = subagents_args.pop("command", "subagents.list")
            text = await asyncio.to_thread(
                lambda: (
                    run_command(
                        command_name, BemineSettings.load(), subagents_args
                    ).text
                )
            )
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        smm_args = _direct_smm_command(normalized_lower)
        if smm_args is not None:
            command_name = smm_args.pop("command", "smm.draft")
            text = await asyncio.to_thread(
                lambda: run_command(command_name, BemineSettings.load(), smm_args).text
            )
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        phantom_args = _direct_phantom_command(normalized_lower)
        if phantom_args is not None:
            command_name = phantom_args.pop("command", "phantom.status")
            text = await asyncio.to_thread(
                lambda: (
                    run_command(command_name, BemineSettings.load(), phantom_args).text
                )
            )
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        backlog_args = _direct_backlog_command(normalized_lower)
        if backlog_args is not None:
            command_name = backlog_args.pop("command", "backlog.list")
            text = await asyncio.to_thread(
                lambda: (
                    run_command(command_name, BemineSettings.load(), backlog_args).text
                )
            )
            await responder.send(_discord_chunk(text, limit=1900)[0])
            return
        blog_args = _direct_blog_command(normalized_lower)
        if blog_args is not None:
            command_name = blog_args.pop("command", "blog.list")
            text = await asyncio.to_thread(
                lambda: run_command(command_name, BemineSettings.load(), blog_args).text
            )
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
            channel_key = str(message.channel.id)
            from bemine.db import BemineDB as _BemineDB

            _wdb = _BemineDB(self.settings.db_path)
            conversation_context = self._conversation_context(message)
            try:
                backlog_entry = await asyncio.to_thread(
                    maybe_add_backlog_from_discord_message,
                    BemineSettings.load(),
                    _wdb,
                    message,
                    normalized,
                )
            except Exception as exc:
                await responder.send(f"Backlog capture failed: {exc}")
                return
            if backlog_entry is not None:
                await responder.send(
                    f"Added `{backlog_entry.owner}` backlog item: `{backlog_entry.path}`"
                )
                return
            try:
                goal_result = await asyncio.to_thread(
                    maybe_start_goal_from_discord_message,
                    BemineSettings.load(),
                    _wdb,
                    message,
                    normalized,
                    conversation_context=conversation_context,
                )
            except Exception as exc:
                await responder.send(f"Goal start failed: {exc}")
                return
            if goal_result is not None:
                await responder.send(render_goal_start(goal_result))
                return
            if await asyncio.to_thread(
                maybe_consume_watcher_reply,
                BemineSettings.load(),
                _wdb,
                channel_id=channel_key,
                text=normalized,
            ):
                await responder.send("↪ Relayed your reply into the watched session.")
                return
            # If a dispatch is already running in this channel, interrupt it and fold in
            # the newer message (signal the worker to stop, cancel the awaiting task).
            prev_task = self._dispatch_tasks.get(channel_key)
            prev_event = self._dispatch_cancel_events.get(channel_key)
            if prev_task is not None and not prev_task.done():
                if prev_event is not None:
                    prev_event.set()
                prev_task.cancel()
                try:
                    await responder.send(
                        "↻ New message — interrupting the previous run and restarting with this one."
                    )
                except discord.DiscordException:
                    pass

            cancel_event = threading.Event()
            self._dispatch_cancel_events[channel_key] = cancel_event
            task = asyncio.ensure_future(
                self._run_dispatch(
                    message, normalized, saved_attachments, responder, cancel_event
                )
            )
            self._dispatch_tasks[channel_key] = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            return

        await self.bot.process_commands(message)

    async def _run_dispatch(
        self,
        message: discord.Message,
        normalized: str,
        saved_attachments: list[Path],
        responder: "DiscordResponder",
        cancel_event: threading.Event,
    ) -> None:
        await _acknowledge_message(message, emoji=WORKING_REACTION)
        loop = asyncio.get_running_loop()
        reporter = _ProgressReporter(loop, message.channel)
        try:
            final_text = await asyncio.to_thread(
                run_bound_agent,
                BemineSettings.load(),
                message,
                normalized,
                saved_attachments,
                reporter,
                cancel_event,
            )
        except asyncio.CancelledError:
            await reporter.finish(note="↻ Interrupted.")
            raise
        except Exception as exc:
            error = _redact(str(exc))
            await reporter.finish(note=f"Dispatch failed: {error}")
            await _acknowledge_message(message, emoji=WARNING_REACTION)
            await responder.send(f"Dispatch failed: {error}")
            return
        # If a newer message superseded this run, don't post a stale answer.
        if cancel_event.is_set():
            await reporter.finish(note="↻ Interrupted.")
            return
        await reporter.finish(note=None)
        await _acknowledge_message(message, emoji=SUCCESS_REACTION)
        await responder.send_chunks(final_text or "(no output)", limit=1900)

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
            channel_name=_message_channel_name(message),
            category_name=_message_category_name(message),
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


def _direct_blog_command(normalized_lower: str) -> dict[str, str] | None:
    text = re.sub(r"[\s.?!]+$", "", normalized_lower.strip().lower())
    variants = {
        text,
        text.removeprefix("!mayor ").removeprefix("mayor "),
        text.removeprefix("!bemine ").removeprefix("bemine "),
    }
    if variants & {
        "blog",
        "blog queue",
        "blog list",
        "list blogs",
        "show blog queue",
        "what blogs are queued",
        "what's in the blog queue",
        "what is in the blog queue",
    }:
        return {"command": "blog.list"}
    for raw in variants:
        match = re.fullmatch(r"publish blog ([a-z0-9-]+)", raw)
        if match:
            return {"command": "blog.publish", "slug": match.group(1)}
        match = re.fullmatch(r"(?:show|review) blog ([a-z0-9-]+)", raw)
        if match:
            return {"command": "blog.show", "slug": match.group(1)}
        match = re.fullmatch(
            r"request changes on ([a-z0-9-]+)[:,]?\s+(.+)", raw, re.DOTALL
        )
        if match:
            return {
                "command": "blog.request_changes",
                "slug": match.group(1),
                "note": match.group(2).strip(),
            }
    return None


def _direct_backlog_command(normalized_lower: str) -> dict[str, str] | None:
    text = re.sub(r"[\s.?!]+$", "", normalized_lower.strip().lower())
    variants = {
        text,
        text.removeprefix("!mayor ").removeprefix("mayor "),
        text.removeprefix("!bemine ").removeprefix("bemine "),
    }
    if variants & {
        "backlog status",
        "backlog health",
        "how many backlog items",
        "how many backlog items are open",
        "how much backlog is open",
        "what backlog is open",
    }:
        return {"command": "backlog.status"}
    if variants & {
        "pa backlog status",
        "pa backlog health",
        "how many pa backlog items",
        "how many pa backlog items are open",
    }:
        return {"command": "backlog.status", "owner": "PA"}
    if variants & {
        "smm backlog status",
        "smm backlog health",
        "how many smm backlog items",
        "how many smm backlog items are open",
    }:
        return {"command": "backlog.status", "owner": "SMM"}
    if variants & {
        "backlog",
        "backlog list",
        "show backlog",
        "show my backlog",
        "open backlog",
        "open backlog items",
        "my backlog",
        "what is in my backlog",
        "what's in my backlog",
        "what is in the backlog",
        "what's in the backlog",
    }:
        return {"owner": default_backlog_owner()}
    if variants & {
        "pa backlog",
        "show pa backlog",
        "open pa backlog",
        "what is in pa backlog",
        "what's in pa backlog",
    }:
        return {"owner": "PA"}
    if variants & {
        "smm backlog",
        "show smm backlog",
        "open smm backlog",
        "what is in smm backlog",
        "what's in smm backlog",
    }:
        return {"owner": "SMM"}
    return None


def _direct_subagents_command(normalized_lower: str) -> dict[str, str] | None:
    text = re.sub(r"[\s.?!]+$", "", normalized_lower.strip().lower())
    variants = {
        text,
        text.removeprefix("!mayor ").removeprefix("mayor "),
        text.removeprefix("!bemine ").removeprefix("bemine "),
    }
    if variants & {
        "subagents",
        "subagents list",
        "list subagents",
        "show subagents",
        "show subagent profiles",
        "what subagents are configured",
        "what subagents do you have",
        "what subagent profiles are loaded",
    }:
        return {"command": "subagents.list"}
    if variants & {
        "show pa subagent",
        "show pa profile",
        "show pa.md",
        "show pa",
        "what is pa",
        "what does pa do",
        "what is pa for",
        "what is pa.md",
        "what is the pa subagent",
    }:
        return {"command": "subagents.show", "name": "PA"}
    if variants & {
        "show smm subagent",
        "show smm profile",
        "show smm.md",
        "show smm",
        "what is smm",
        "what does smm do",
        "what is smm for",
        "what is smm.md",
        "what is the smm subagent",
    }:
        return {"command": "subagents.show", "name": "SMM"}
    if variants & {
        "show style",
        "show style subagent",
        "show style.md",
        "what is style.md",
        "what is the style subagent",
        "what is the style convention",
        "what style should smm follow",
        "what style should i follow",
    }:
        return {"command": "subagents.show", "name": "style"}
    return None


def _direct_smm_command(normalized_lower: str) -> dict[str, str] | None:
    text = re.sub(r"[\s.?!]+$", "", normalized_lower.strip().lower())
    variants = {
        text,
        text.removeprefix("!mayor ").removeprefix("mayor "),
        text.removeprefix("!bemine ").removeprefix("bemine "),
    }
    if variants & {
        "smm status",
        "smm draft status",
        "social draft status",
        "post draft status",
        "when was the last smm draft",
        "when was the last social draft",
        "when was the last post draft",
        "last smm draft",
        "latest smm draft",
        "are smm drafts fresh",
        "ampixa assets",
        "smm assets",
        "social assets",
        "asset status",
        "what ampixa assets are installed",
        "what smm assets do we have",
    }:
        return {"command": "smm.status"}
    if variants & {
        "smm",
        "smm draft",
        "draft a post",
        "draft post",
        "what should i post",
        "what should i post about",
        "what should i post today",
        "what should i post this week",
        "what can i post",
        "what can i post today",
        "what should smm post",
        "what should smm post about",
        "what should smm post this week",
        "what should smm post today",
        "give me a post idea",
        "show me post ideas",
        "show me social post ideas",
        "post ideas",
    }:
        return {"command": "smm.draft"}
    return None


def _direct_phantom_command(normalized_lower: str) -> dict[str, str] | None:
    text = re.sub(r"[\s.?!]+$", "", normalized_lower.strip().lower())
    variants = {
        text,
        text.removeprefix("!mayor ").removeprefix("mayor "),
        text.removeprefix("!bemine ").removeprefix("bemine "),
    }
    if variants & {
        "phantom",
        "phantom status",
        "show phantom",
        "phone status",
        "what is phantom",
        "what does phantom do",
        "what is phantom for",
        "what is the phantom app",
        "what phantom is configured",
        "what phantom status is",
        "what is phantom status",
        "what is phantom install state",
    }:
        return {"command": "phantom.status"}
    if variants & {
        "phantom build",
        "build phantom",
        "build phantom apk",
    }:
        return {"command": "phantom.build"}
    if variants & {
        "phantom install",
        "install phantom",
        "install phantom app",
    }:
        return {"command": "phantom.install"}
    return None


def _direct_observability_command(normalized_lower: str) -> str:
    text = re.sub(r"[\s.?!]+$", "", normalized_lower.strip().lower())
    variants = {
        text,
        text.removeprefix("!mayor ").removeprefix("mayor "),
        text.removeprefix("!bemine ").removeprefix("bemine "),
    }
    if variants & {
        "health",
        "health status",
        "what is running",
        "what's running",
        "what daemons are running",
        "what services are running",
    }:
        return "health"
    if variants & {
        "help",
        "bemine help",
        "what can you do",
        "what do you do",
        "what are you",
        "what is bemine",
        "what is this",
        "what is this for",
        "what is this bot for",
        "list commands",
        "commands",
    }:
        return "help"
    if variants & {
        "config",
        "config show",
        "show config",
        "what config is set",
        "what's my config",
        "what config do i have",
        "what backlog owners are configured",
        "what is the default backlog owner",
        "what's the default backlog owner",
        "what backlog config is set",
        "what backlog config is configured",
    }:
        return "config.show"
    if variants & {
        "routes",
        "discord routes",
        "channel routes",
        "discord channels",
        "managed channels",
        "what channels are managed",
    }:
        return "discord.routes"
    if variants & {
        "discord sync plan",
        "channel sync plan",
        "project channel sync plan",
        "what channels would be created",
        "what discord channels would be created",
        "what channel sync would do",
    }:
        return "discord.sync_plan"
    if variants & {
        "continuity",
        "continuity status",
        "line cuts",
        "line cut status",
        "power loss",
        "power loss status",
        "wake sessions on shutdown",
        "wake sessions after shutdown",
        "wake sessions after reboot",
        "will sessions resume after reboot",
        "will sessions resume after shutdown",
        "do sessions resume after reboot",
        "do sessions resume after shutdown",
        "reboot recovery",
        "shutdown recovery",
    }:
        return "continuity"
    if variants & {
        "dispatch",
        "dispatch status",
        "model",
        "model status",
        "what model are we using",
        "what is the model we are using",
        "what model are we running",
        "what model are we running right now",
    }:
        return "dispatch.status"
    if variants & {
        "pa status",
        "pa evidence",
        "pa evidence status",
        "what evidence does pa use",
        "what is pa looking at",
        "what sources does pa use",
        "pa sources",
    }:
        return "pa.status"
    if variants & {
        "pa",
        "pa plan",
        "focus",
        "focus plan",
        "what should i focus on",
        "what should i focus on today",
        "what should i focus on this week",
        "what should i do next",
        "what should i focus on tomorrow",
    }:
        return "pa.plan"
    if variants & {
        "dream status",
        "dream freshness",
        "when did you last dream",
        "when was the last dream",
        "last dream",
        "latest dream",
        "is dream fresh",
    }:
        return "dream.status"
    if variants & {
        "dream",
        "dream run",
        "run dream",
        "dream now",
        "learn from recent history",
        "record a dream",
        "record dream",
    }:
        return "dream.run"
    if variants & {
        "brief status",
        "pa/smm brief status",
        "pa smm brief status",
        "brief freshness",
        "when was the last pa/smm brief",
        "when was the last pa smm brief",
        "last pa/smm brief",
        "latest pa/smm brief",
        "is pa/smm brief fresh",
    }:
        return "brief.status"
    if variants & {
        "brief",
        "brief run",
        "run brief",
        "generate brief",
        "generate pa/smm brief",
        "generate pa smm brief",
        "pa/smm brief",
        "pa smm brief",
    }:
        return "brief.run"
    if variants & {"goals", "goal status", "goals status"}:
        return "goals.list"
    if variants & {
        "goal sessions",
        "goal session",
        "active goal sessions",
        "list goal sessions",
        "what goal sessions are active",
        "what goals are active",
    }:
        return "goals.list"
    if variants & {
        "jobs",
        "jobs list",
        "job list",
        "cron jobs",
        "scheduled jobs",
        "what jobs are scheduled",
        "what cron jobs are set up",
    }:
        return "jobs.list"
    return ""


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
