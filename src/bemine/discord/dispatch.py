from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from bemine.config import BemineSettings, config_int, config_path, config_string
from bemine.db import BemineDB
from bemine.context.harness_context import load_harness_context
from bemine.context.subagents import load_subagent_context
from bemine.discord.store import Store
from bemine.kimi_handler import (
    KimiHandler,
    JsonTransport,
    load_kimi_config,
    resolve_kimi_api_key,
)
from bemine.workers.native_runtime import KimiNativeRuntime
from bemine.context.operator_context import load_operator_context
from bemine.index.project_filters import ignored_project_reason
from bemine.index.status import _redact

PROJECT_CATEGORY_NAME = "Bemine Projects"
ARCHIVE_CATEGORY_NAME = "Bemine Archive"
DISPATCH_CONFIG_NAME = "discord-projects.json"
MAX_CHANNELS_PER_CATEGORY = 50

Runner = Callable[..., subprocess.CompletedProcess[str]]
ProgressCallback = Callable[["DispatchProgress"], None]


class DispatchInterrupted(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectChannel:
    channel_name: str
    cwd: Path
    project_id: str = ""
    project_name: str = ""
    runtime: str = ""


@dataclass(frozen=True)
class DispatchRequest:
    channel_id: str
    channel_name: str
    message_id: str
    author_name: str
    content: str
    attachments: list[Path]
    route_reason: str = ""
    conversation_context: str = ""
    cancel_event: threading.Event | None = None


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    project: ProjectChannel
    output: str
    elapsed_seconds: float
    returncode: int | None = None
    timed_out: bool = False
    interrupted: bool = False


@dataclass(frozen=True)
class DispatchProgress:
    runtime: str
    event_type: str
    summary: str
    raw_event: Mapping[str, Any]


def _dispatch_cancelled(request: DispatchRequest) -> bool:
    return bool(request.cancel_event and request.cancel_event.is_set())


def _interrupted_result(project: ProjectChannel, started: float) -> DispatchResult:
    return DispatchResult(
        ok=False,
        project=project,
        output="Dispatch interrupted by a newer Discord message.",
        elapsed_seconds=time.monotonic() - started,
        returncode=None,
        interrupted=True,
    )


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _emit_progress(
    progress_callback: ProgressCallback | None,
    *,
    runtime: str,
    event_type: str,
    summary: str,
    raw_event: Mapping[str, Any],
) -> None:
    if progress_callback:
        progress_callback(
            DispatchProgress(
                runtime=runtime,
                event_type=event_type,
                summary=summary,
                raw_event=raw_event,
            )
        )


class DispatchBackend(Protocol):
    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> DispatchResult: ...


@dataclass(frozen=True)
class ProjectRoute:
    project: ProjectChannel
    reason: str


def load_project_channels(
    state_dir: Path,
    *,
    repo_dir: Path | None = None,
    home: Path | None = None,
) -> dict[str, ProjectChannel]:
    repo_dir = (repo_dir or Path.cwd()).expanduser().resolve()
    home = (home or Path.home()).expanduser().resolve()
    mapping = _default_project_channels(repo_dir=repo_dir, home=home)

    config_path = state_dir / DISPATCH_CONFIG_NAME
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path} must contain a JSON object")
        for name, value in raw.items():
            channel = _project_channel_from_config(str(name), value)
            if channel.cwd.exists() and not ignored_project_reason(
                channel.cwd, name=channel.project_name
            ):
                mapping[_channel_key(name)] = channel
    return mapping


def project_channels_from_db(db: BemineDB, *, limit: int = 200) -> list[ProjectChannel]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT project_id, name, cwd, last_seen_at
            FROM projects
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    rows = [
        dict(row)
        for row in rows
        if not ignored_project_reason(str(row["cwd"]), name=str(row["name"]))
    ]
    return _project_channels_from_rows(rows)


def write_project_channel_config(
    state_dir: Path,
    channels: list[ProjectChannel],
    *,
    aliases: Mapping[str, ProjectChannel] | None = None,
    preserve_existing: bool = True,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / DISPATCH_CONFIG_NAME
    payload: dict[str, dict[str, str]] = {}
    if preserve_existing and path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name, value in raw.items():
                try:
                    existing = _project_channel_from_config(str(name), value)
                except (KeyError, TypeError, ValueError):
                    continue
                if not ignored_project_reason(existing.cwd, name=existing.project_name):
                    payload[existing.channel_name] = _project_channel_payload(existing)
    for channel in channels:
        payload[channel.channel_name] = _project_channel_payload(channel)
    for alias_name, channel in (aliases or {}).items():
        alias = ProjectChannel(
            channel_name=alias_name,
            cwd=channel.cwd,
            project_id=channel.project_id,
            project_name=channel.project_name,
        )
        payload[alias.channel_name] = _project_channel_payload(alias)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def resolve_project_channel(
    *,
    channel_name: str,
    category_name: str,
    state_dir: Path,
    repo_dir: Path | None = None,
) -> ProjectChannel | None:
    if not _is_project_category_name(category_name):
        return None
    return load_project_channels(state_dir, repo_dir=repo_dir).get(
        _channel_key(channel_name)
    )


def infer_project_route(
    *,
    channel_name: str,
    category_name: str,
    content: str,
    state_dir: Path,
    repo_dir: Path | None = None,
    mentioned_channels: Iterable[tuple[str, str]] = (),
    allow_default: bool = True,
) -> ProjectRoute | None:
    project = resolve_project_channel(
        channel_name=channel_name,
        category_name=category_name,
        state_dir=state_dir,
        repo_dir=repo_dir,
    )
    if project:
        return ProjectRoute(project=project, reason="project-channel")

    for mentioned_name, mentioned_category in mentioned_channels:
        mentioned_project = resolve_project_channel(
            channel_name=mentioned_name,
            category_name=mentioned_category,
            state_dir=state_dir,
            repo_dir=repo_dir,
        )
        if mentioned_project:
            return ProjectRoute(
                project=mentioned_project, reason=f"mentioned-channel:{mentioned_name}"
            )

    channels = load_project_channels(state_dir, repo_dir=repo_dir)
    channel_match = channels.get(_channel_key(channel_name))
    if channel_match:
        return ProjectRoute(
            project=channel_match, reason=f"channel-name:{channel_name}"
        )

    content_match = _project_from_content(content, channels)
    if content_match:
        return ProjectRoute(project=content_match, reason="content-project-reference")

    if allow_default:
        fallback = channels.get("bemine") or channels.get("mayor")
        if fallback:
            return ProjectRoute(project=fallback, reason="default-mayor")
    return None


def ensure_default_project_config(
    state_dir: Path,
    *,
    repo_dir: Path | None = None,
    home: Path | None = None,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / DISPATCH_CONFIG_NAME
    if path.exists():
        return path
    defaults = load_project_channels(state_dir, repo_dir=repo_dir, home=home)
    payload = {
        channel.channel_name: {"cwd": str(channel.cwd)} for channel in defaults.values()
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _project_channel_topic(channel: ProjectChannel, device: str = "") -> str:
    name = channel.project_name or channel.channel_name
    where = f" on `{device}`" if device else ""
    return (
        f"Bemine project `{name}` — root {channel.cwd}{where}. Talk here to operate "
        "this project; Bemine injects current project state (sessions, handoffs, "
        "workers) each turn."
    )[:1024]


def _device_category_name(device: str) -> str:
    return f"Bemine · {device}"


def _cfg_excluded_channel_names() -> set[str]:
    raw = config_string("discord", "channel_exclude_names", "")
    if raw.strip():
        return {_channel_key(part) for part in raw.split(",") if part.strip()}
    return {"downloads", "codex-cwd", "tmp", "desktop", "documents"}


def _fleet_channel_candidates(settings: BemineSettings) -> dict[str, dict[str, Any]]:
    """Live fleet attribution: best (most recently active) device per cwd.

    Channels are organized by the computer the project actually runs on; the
    index is local-only, so remote projects are discovered via fleet status."""
    from bemine.index.project_filters import ignored_project_reason
    from bemine.index.status import collect_fleet_status

    rows, _errors = collect_fleet_status(settings, limit_per_runtime=25)
    exclude = _cfg_excluded_channel_names()
    best: dict[str, dict[str, Any]] = {}
    for status in rows:
        cwd = (status.cwd or "").strip()
        if not cwd:
            continue
        try:
            if ignored_project_reason(cwd):
                continue
        except Exception:
            continue
        name = _channel_key(Path(cwd).name)
        if not name or name in exclude:
            continue
        device = (
            settings.device_id
            if status.device_id in {"", "local"}
            else str(status.device_id)
        )
        age = int(status.age_seconds if status.age_seconds is not None else 10**9)
        current = best.get(cwd)
        if current is None or age < int(current["age"]):
            best[cwd] = {"cwd": cwd, "device": device, "age": age, "name": name}
    return best


def _device_channel_layout(
    settings: BemineSettings,
    channels: list[ProjectChannel],
    aliases: Mapping[str, ProjectChannel],
    attribution: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Decide each channel's device and which remote-only channels to add.

    Local channels follow their latest fleet activity (default: this device).
    Remote cwds with recent activity and an unclaimed name become new channels."""
    device_for: dict[str, str] = {}
    for channel in channels:
        hit = attribution.get(str(channel.cwd))
        device_for[channel.channel_name] = (
            str(hit["device"]) if hit else settings.device_id
        )
    for alias_name, alias_channel in aliases.items():
        device_for[alias_name] = device_for.get(
            alias_channel.channel_name, settings.device_id
        )
    window_secs = (
        max(1, config_int("discord", "fleet_channel_max_age_days", 14)) * 86400
    )
    taken = {_channel_key(name) for name in device_for}
    remote_entries: list[dict[str, Any]] = []
    for info in sorted(attribution.values(), key=lambda item: int(item["age"])):
        if str(info["device"]) == settings.device_id:
            continue
        if int(info["age"]) > window_secs:
            continue
        name = str(info["name"])
        if name in taken:
            continue
        taken.add(name)
        remote_entries.append(info)
        device_for[name] = str(info["device"])
    return device_for, remote_entries


async def _modify_discord_channel(
    session: aiohttp.ClientSession, channel_id: str, payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    return await _discord_json(
        session,
        "PATCH",
        f"https://discord.com/api/v10/channels/{channel_id}",
        json_payload=dict(payload),
    )


async def _ensure_archive_category(
    session: aiohttp.ClientSession,
    guild_id: str,
    existing_channels: list[Mapping[str, Any]],
) -> Mapping[str, Any]:
    key = _channel_key(ARCHIVE_CATEGORY_NAME)
    for channel in existing_channels:
        if (
            channel.get("type") == 4
            and _channel_key(str(channel.get("name") or "")) == key
        ):
            return channel
    return await _create_project_category(session, guild_id, ARCHIVE_CATEGORY_NAME)


async def sync_project_channels_to_discord(
    settings: BemineSettings,
    discord_token: str,
    guild_id: str,
    *,
    limit: int = 200,
    prune: bool = False,
    archive_stale: bool = False,
) -> dict[str, Any]:
    db = BemineDB(settings.db_path)
    channels, aliases = _desired_project_channels(db, limit=limit)
    config_path = write_project_channel_config(
        settings.state_dir,
        channels,
        aliases=aliases,
        preserve_existing=False,
    )
    target_names = {channel.channel_name for channel in channels}
    target_names.update(aliases.keys())

    # Organize by the computer each project runs on (live fleet attribution);
    # remote-device projects get channels too.
    attribution = _fleet_channel_candidates(settings)
    device_for, remote_entries = _device_channel_layout(
        settings, channels, aliases, attribution
    )
    remote_channels = [
        ProjectChannel(
            channel_name=str(info["name"]),
            cwd=Path(str(info["cwd"])),
            project_id=f"remote:{info['device']}:{info['name']}",
            project_name=str(info["name"]),
        )
        for info in remote_entries
    ]
    target_names.update(channel.channel_name for channel in remote_channels)

    headers = {
        "Authorization": f"Bot {discord_token}",
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": "Bemine per-project channel sync",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        existing = await _discord_json(
            session, "GET", f"https://discord.com/api/v10/guilds/{guild_id}/channels"
        )
        if not isinstance(existing, list):
            raise RuntimeError("Discord channel list response was not a list")
        legacy_categories = [
            channel
            for channel in existing
            if channel.get("type") == 4
            and _is_project_category_name(str(channel.get("name") or ""))
        ]
        device_categories: dict[str, Mapping[str, Any]] = {
            str(channel.get("name") or "").lower(): channel
            for channel in existing
            if channel.get("type") == 4
            and str(channel.get("name") or "").lower().startswith("bemine ·")
        }
        managed_category_ids = {
            str(channel.get("id") or "") for channel in legacy_categories
        } | {str(channel.get("id") or "") for channel in device_categories.values()}
        existing_by_name = {
            str(channel.get("name")): channel
            for channel in existing
            if channel.get("type") == 0
            and (
                str(channel.get("parent_id") or "") in managed_category_ids
                # ADOPT same-name uncategorized channels (the session watcher
                # creates project channels bare) — creating alongside them made
                # empty duplicates while the originals kept the history
                or (
                    not channel.get("parent_id")
                    and str(channel.get("name")) in target_names
                )
            )
        }

        async def _device_category(device: str) -> Mapping[str, Any]:
            key = _device_category_name(device).lower()
            category = device_categories.get(key)
            if category is None:
                category = await _create_project_category(
                    session, guild_id, _device_category_name(device)
                )
                device_categories[key] = category
            return category

        topic_by_name = {
            channel.channel_name: _project_channel_topic(
                channel, device_for.get(channel.channel_name, "")
            )
            for channel in [*channels, *remote_channels]
        }
        for alias_name, alias_channel in aliases.items():
            topic_by_name[alias_name] = _project_channel_topic(
                alias_channel, device_for.get(alias_name, "")
            )
        deleted: list[dict[str, str]] = []
        if prune:
            for name, channel in list(existing_by_name.items()):
                if name in target_names:
                    continue
                await _delete_discord_channel(session, str(channel.get("id") or ""))
                deleted.append({"id": str(channel.get("id") or ""), "name": name})
                existing_by_name.pop(name, None)
        archived: list[dict[str, str]] = []
        if archive_stale and not prune:
            archive_category: Mapping[str, Any] | None = None
            for name, channel in list(existing_by_name.items()):
                if name in target_names:
                    continue
                if archive_category is None:
                    archive_category = await _ensure_archive_category(
                        session, guild_id, existing
                    )
                await _modify_discord_channel(
                    session,
                    str(channel.get("id") or ""),
                    {"parent_id": str(archive_category.get("id") or "")},
                )
                archived.append({"id": str(channel.get("id") or ""), "name": name})
                existing_by_name.pop(name, None)
        topics_updated: list[str] = []
        moved: list[dict[str, str]] = []
        for name, channel in existing_by_name.items():
            device = device_for.get(name, settings.device_id)
            category = await _device_category(device)
            payload: dict[str, Any] = {}
            if str(channel.get("parent_id") or "") != str(category.get("id") or ""):
                payload["parent_id"] = str(category.get("id") or "")
            desired_topic = topic_by_name.get(name, "")
            if desired_topic and str(channel.get("topic") or "") != desired_topic:
                payload["topic"] = desired_topic
            if payload:
                await _modify_discord_channel(
                    session, str(channel.get("id") or ""), payload
                )
                if "parent_id" in payload:
                    moved.append({"name": name, "device": device})
                if "topic" in payload:
                    topics_updated.append(name)
        created: list[dict[str, str]] = []
        skipped: list[str] = []
        for name in sorted(target_names):
            if name in existing_by_name:
                skipped.append(name)
                continue
            device = device_for.get(name, settings.device_id)
            category = await _device_category(device)
            channel = await _create_text_channel(
                session,
                guild_id,
                name,
                parent_id=str(category["id"]),
                topic=topic_by_name.get(name, ""),
            )
            created.append(
                {
                    "id": str(channel.get("id") or ""),
                    "name": name,
                    "category_id": str(category.get("id") or ""),
                    "category_name": str(category.get("name") or ""),
                }
            )
        # every managed channel now lives in a device category; the legacy
        # "Bemine Projects" categories are empty shells — remove them
        deleted_categories: list[dict[str, str]] = []
        for category in legacy_categories:
            await _delete_discord_channel(session, str(category.get("id") or ""))
            deleted_categories.append(
                {
                    "id": str(category.get("id") or ""),
                    "name": str(category.get("name") or ""),
                }
            )
        categories = list(device_categories.values())

    db.record_audit_event(
        category="discord",
        event_type="project_channels_synced",
        actor="channel_sync",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {
                "guild_id": guild_id,
                "category_ids": [
                    str(category.get("id") or "") for category in categories
                ],
                "created": created,
                "deleted": deleted,
                "archived": archived,
                "topics_updated": topics_updated,
                "deleted_categories": deleted_categories,
                "skipped_count": len(skipped),
                "config_path": str(config_path),
                "prune": prune,
                "archive_stale": archive_stale,
            },
            sort_keys=True,
        ),
    )
    return {
        "guild_id": guild_id,
        "category_ids": [str(category.get("id") or "") for category in categories],
        "category_names": [
            str(category.get("name") or PROJECT_CATEGORY_NAME)
            for category in categories
        ],
        "created": created,
        "deleted": deleted,
        "archived": archived,
        "topics_updated": topics_updated,
        "deleted_categories": deleted_categories,
        "skipped": skipped,
        "config_path": str(config_path),
        "project_count": len(channels),
    }


def plan_project_channel_sync(
    settings: BemineSettings,
    *,
    limit: int = 200,
    prune: bool = False,
    archive_stale: bool = False,
) -> dict[str, Any]:
    db = BemineDB(settings.db_path)
    channels, aliases = _desired_project_channels(db, limit=limit)
    desired = _desired_channel_entries(channels, aliases)
    desired_names = {str(row["channel_name"]) for row in desired}
    cached_channels = [
        row
        for row in Store(settings.db_path).channel_metadata()
        if _is_project_category_name(str(row.get("category_name") or ""))
        and int(row.get("channel_type") or 0) == 0
    ]
    existing_by_name = {
        str(row.get("channel_name") or ""): row
        for row in cached_channels
        if str(row.get("channel_name") or "")
    }
    existing_names = set(existing_by_name)
    would_create = [
        row for row in desired if str(row["channel_name"]) not in existing_names
    ]
    existing_matches = [
        _cached_channel_entry(existing_by_name[name])
        for name in sorted(desired_names & existing_names)
    ]
    stale = [
        _cached_channel_entry(row)
        for name, row in sorted(existing_by_name.items())
        if name not in desired_names
    ]
    return {
        "dry_run": True,
        "metadata_source": "local_discord_channels_cache",
        "config_path": str(settings.state_dir / DISPATCH_CONFIG_NAME),
        "prune": prune,
        "project_count": len(channels),
        "alias_count": len(aliases),
        "desired_count": len(desired),
        "existing_count": len(existing_by_name),
        "would_create": would_create,
        "existing": existing_matches,
        "stale": stale,
        "would_prune": stale if prune else [],
        "archive_stale": archive_stale,
        "would_archive": stale if (archive_stale and not prune) else [],
    }


class ProjectDispatcher:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        codex_dispatcher: DispatchBackend | None = None,
        kimi_dispatcher: DispatchBackend | None = None,
    ) -> None:
        self.settings = settings
        self.codex_dispatcher = codex_dispatcher
        self.kimi_dispatcher = kimi_dispatcher

    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> DispatchResult:
        runtime = resolve_dispatch_runtime(project.runtime)
        if runtime == "kimi":
            dispatcher = self.kimi_dispatcher or resolve_kimi_dispatcher(self.settings)
            return dispatcher.dispatch(project, request, progress_callback)
        if runtime == "codex":
            dispatcher = self.codex_dispatcher or resolve_codex_dispatcher(
                self.settings
            )
            return dispatcher.dispatch(project, request, progress_callback)
        if runtime == "claude":
            dispatcher = resolve_claude_dispatcher(self.settings)
            return dispatcher.dispatch(project, request, progress_callback)
        return DispatchResult(
            ok=False,
            project=project,
            output=(
                f"Unsupported dispatch runtime `{runtime}` for `#{project.channel_name}`. "
                "Use `kimi`, `codex`, or `claude`."
            ),
            elapsed_seconds=0.0,
            returncode=None,
        )


class KimiDispatcher:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        runner: Runner | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        timeout_seconds: int = 900,
        kimi_bin: str | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.popen_factory = popen_factory or subprocess.Popen
        self.timeout_seconds = timeout_seconds
        self.kimi_bin = kimi_bin or resolve_kimi_bin()
        self.config = load_kimi_config(settings.hermes_home)
        self.db = BemineDB(settings.db_path)

    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> DispatchResult:
        started = time.monotonic()
        if _dispatch_cancelled(request):
            return _interrupted_result(project, started)
        self._audit(
            "kimi_dispatch_started",
            project,
            request,
            {
                "timeout_seconds": self.timeout_seconds,
                "kimi_bin": self.kimi_bin,
                "model": self.config.model,
            },
        )
        prompt = build_kimi_prompt(project, request)
        cli_config = render_kimi_cli_config(self.settings, self.config)
        env = os.environ.copy()
        api_key = resolve_kimi_api_key(self.settings.hermes_home, self.config)
        if api_key:
            env[self.config.api_key_env] = api_key
            env["KIMI_API_KEY"] = api_key
        env["KIMI_BASE_URL"] = _kimi_cli_base_url(self.config.base_url)
        command = [
            self.kimi_bin,
            "--work-dir",
            str(project.cwd),
            "--config-file",
            str(cli_config),
            "--model",
            "bemine",
            "--yolo",
            "--print",
            "--output-format",
            "text",
            "--final-message-only",
            "--prompt",
            prompt,
        ]
        _emit_progress(
            progress_callback,
            runtime="kimi",
            event_type="cli.started",
            summary=f"🧰 kimi cli: `{_compact_progress_value(' '.join(command[:3]))}`",
            raw_event={"command": command, "cwd": str(project.cwd)},
        )
        if self.runner is None:
            return self._dispatch_with_popen(
                command=command,
                env=env,
                started=started,
                project=project,
                request=request,
                progress_callback=progress_callback,
            )
        try:
            proc = self.runner(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                env=env,
            )
            if _dispatch_cancelled(request):
                result = _interrupted_result(project, started)
                self._audit_result("kimi_dispatch_finished", project, request, result)
                _emit_progress(
                    progress_callback,
                    runtime="kimi",
                    event_type="cli.interrupted",
                    summary="⚠️ kimi cli interrupted by newer message",
                    raw_event={"returncode": None},
                )
                return result
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=(
                    f"Kimi dispatch timed out after {self.timeout_seconds}s for "
                    f"`#{project.channel_name}`."
                ),
                elapsed_seconds=elapsed,
                timed_out=True,
            )
            self._audit_result("kimi_dispatch_finished", project, request, result)
            return result
        except FileNotFoundError as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=(
                    "Kimi dispatch could not start because the `kimi` binary was not found. "
                    "Set `BEMINE_KIMI_BIN` or `kimi.bin` in `bemine.toml`."
                ),
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "kimi_dispatch_finished",
                project,
                request,
                result,
                extra={"error": str(exc), "kimi_bin": self.kimi_bin},
            )
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=f"Kimi dispatch failed before completion: {_redact(str(exc))}",
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "kimi_dispatch_finished",
                project,
                request,
                result,
                extra={"error_type": type(exc).__name__, "error": _redact(str(exc))},
            )
            return result

        output = _redact(
            proc.stdout.strip()
            or proc.stderr.strip()
            or "Kimi finished without a final message."
        )
        elapsed = time.monotonic() - started
        result = DispatchResult(
            ok=proc.returncode == 0,
            project=project,
            output=output,
            elapsed_seconds=elapsed,
            returncode=proc.returncode,
        )
        _emit_progress(
            progress_callback,
            runtime="kimi",
            event_type="cli.completed",
            summary=(
                "✅ kimi cli done"
                if proc.returncode == 0
                else f"⚠️ kimi cli exit {proc.returncode}"
            ),
            raw_event={"returncode": proc.returncode},
        )
        self._audit_result(
            "kimi_dispatch_finished",
            project,
            request,
            result,
            extra={
                "model": self.config.model,
                "provider": self.config.provider,
                "cli_config": str(cli_config),
                "stderr": _redact(proc.stderr.strip()[:1000]),
            },
        )
        return result

    def _dispatch_with_popen(
        self,
        *,
        command: list[str],
        env: Mapping[str, str],
        started: float,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None,
    ) -> DispatchResult:
        try:
            proc = self.popen_factory(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
            )
            stdout = ""
            stderr = ""
            deadline = started + self.timeout_seconds
            while True:
                if _dispatch_cancelled(request):
                    _terminate_process(proc)
                    elapsed = time.monotonic() - started
                    result = DispatchResult(
                        ok=False,
                        project=project,
                        output="Kimi CLI dispatch interrupted by a newer Discord message.",
                        elapsed_seconds=elapsed,
                        returncode=proc.poll(),
                        interrupted=True,
                    )
                    self._audit_result(
                        "kimi_dispatch_finished", project, request, result
                    )
                    _emit_progress(
                        progress_callback,
                        runtime="kimi",
                        event_type="cli.interrupted",
                        summary="⚠️ kimi cli interrupted by newer message",
                        raw_event={"returncode": result.returncode},
                    )
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process(proc)
                    raise subprocess.TimeoutExpired(
                        cmd=command, timeout=self.timeout_seconds
                    )
                try:
                    stdout, stderr = proc.communicate(
                        timeout=min(0.25, max(0.01, remaining))
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=(
                    f"Kimi dispatch timed out after {self.timeout_seconds}s for "
                    f"`#{project.channel_name}`."
                ),
                elapsed_seconds=elapsed,
                timed_out=True,
            )
            self._audit_result("kimi_dispatch_finished", project, request, result)
            return result
        except FileNotFoundError as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=(
                    "Kimi dispatch could not start because the `kimi` binary was not found. "
                    "Set `BEMINE_KIMI_BIN` or `kimi.bin` in `bemine.toml`."
                ),
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "kimi_dispatch_finished",
                project,
                request,
                result,
                extra={"error": str(exc), "kimi_bin": self.kimi_bin},
            )
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=f"Kimi dispatch failed before completion: {_redact(str(exc))}",
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "kimi_dispatch_finished",
                project,
                request,
                result,
                extra={"error_type": type(exc).__name__, "error": _redact(str(exc))},
            )
            return result

        output = _redact(
            stdout.strip() or stderr.strip() or "Kimi finished without a final message."
        )
        elapsed = time.monotonic() - started
        result = DispatchResult(
            ok=proc.returncode == 0,
            project=project,
            output=output,
            elapsed_seconds=elapsed,
            returncode=proc.returncode,
        )
        _emit_progress(
            progress_callback,
            runtime="kimi",
            event_type="cli.completed",
            summary=(
                "✅ kimi cli done"
                if proc.returncode == 0
                else f"⚠️ kimi cli exit {proc.returncode}"
            ),
            raw_event={"returncode": proc.returncode},
        )
        self._audit_result(
            "kimi_dispatch_finished",
            project,
            request,
            result,
            extra={
                "model": self.config.model,
                "provider": self.config.provider,
                "cli_config": str(render_kimi_cli_config(self.settings, self.config)),
                "stderr": _redact(stderr.strip()[:1000]),
            },
        )
        return result

    def _audit(
        self,
        event_type: str,
        project: ProjectChannel,
        request: DispatchRequest,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "channel_id": request.channel_id,
            "channel_name": request.channel_name,
            "message_id": request.message_id,
            "project_channel": project.channel_name,
            "project_cwd": str(project.cwd),
            "runtime": "kimi",
            "configured_runtime": project.runtime,
        }
        if extra:
            payload.update(extra)
        self.db.record_audit_event(
            category="discord",
            event_type=event_type,
            actor=f"discord:{request.author_name}",
            device_id=self.settings.device_id,
            payload_json=json.dumps(payload, sort_keys=True),
        )

    def _audit_result(
        self,
        event_type: str,
        project: ProjectChannel,
        request: DispatchRequest,
        result: DispatchResult,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "ok": result.ok,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "interrupted": result.interrupted,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
        }
        if extra:
            payload.update(extra)
        self._audit(event_type, project, request, payload)


class KimiNativeDispatcher:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        transport: JsonTransport | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.settings = settings
        self.runtime = KimiNativeRuntime(
            settings,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        self.db = BemineDB(settings.db_path)

    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> DispatchResult:
        started = time.monotonic()
        session_id = native_session_id(request.channel_id, project.channel_name)
        self._audit(
            "kimi_native_dispatch_started",
            project,
            request,
            {
                "session_id": session_id,
                "model": self.runtime.handler.status().model,
                "backend": "native",
            },
        )
        prompt = build_kimi_native_prompt(project, request)
        system = build_kimi_native_system_prompt(project, request)

        def emit_progress(
            runtime: str,
            event_type: str,
            summary: str,
            raw_event: Mapping[str, Any],
        ) -> None:
            if progress_callback:
                progress_callback(
                    DispatchProgress(
                        runtime=runtime,
                        event_type=event_type,
                        summary=summary,
                        raw_event=raw_event,
                    )
                )

        try:
            native_result = self.runtime.run(
                session_id=session_id,
                channel_id=request.channel_id,
                project_id=project.project_id or project.channel_name,
                cwd=project.cwd,
                prompt=prompt,
                system=system,
                progress_callback=emit_progress,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=f"Kimi native dispatch issue: {_redact(str(exc))}",
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "kimi_native_dispatch_finished",
                project,
                request,
                result,
                {
                    "session_id": session_id,
                    "backend": "native",
                    "error_type": type(exc).__name__,
                    "error": _redact(str(exc)),
                },
            )
            return result

        result = DispatchResult(
            ok=native_result.ok,
            project=project,
            output=_redact(native_result.text),
            elapsed_seconds=native_result.elapsed_seconds,
            returncode=0 if native_result.ok else 1,
        )
        self._audit_result(
            "kimi_native_dispatch_finished",
            project,
            request,
            result,
            {
                "session_id": session_id,
                "backend": "native",
                "model": native_result.model,
                "input_tokens": native_result.input_tokens,
                "output_tokens": native_result.output_tokens,
                "total_tokens": native_result.total_tokens,
            },
        )
        return result

    def _audit(
        self,
        event_type: str,
        project: ProjectChannel,
        request: DispatchRequest,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "channel_id": request.channel_id,
            "channel_name": request.channel_name,
            "message_id": request.message_id,
            "project_channel": project.channel_name,
            "project_cwd": str(project.cwd),
            "runtime": "kimi",
            "configured_runtime": project.runtime,
        }
        if extra:
            payload.update(extra)
        self.db.record_audit_event(
            category="discord",
            event_type=event_type,
            actor=f"discord:{request.author_name}",
            device_id=self.settings.device_id,
            project_id=project.project_id or project.channel_name,
            session_id=str(payload.get("session_id") or ""),
            payload_json=json.dumps(payload, sort_keys=True),
        )

    def _audit_result(
        self,
        event_type: str,
        project: ProjectChannel,
        request: DispatchRequest,
        result: DispatchResult,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "ok": result.ok,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "interrupted": result.interrupted,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
        }
        if extra:
            payload.update(extra)
        self._audit(event_type, project, request, payload)


class UnsupportedConfiguredDispatcher:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        runtime: str,
        backend: str,
        reason: str,
    ) -> None:
        self.settings = settings
        self.runtime = runtime
        self.backend = backend
        self.reason = reason
        self.db = BemineDB(settings.db_path)

    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> DispatchResult:
        del progress_callback
        result = DispatchResult(
            ok=False,
            project=project,
            output=(
                f"`{self.runtime}` backend `{self.backend}` is selected in config, "
                f"but it is not available yet: {self.reason}"
            ),
            elapsed_seconds=0.0,
            returncode=None,
        )
        self.db.record_audit_event(
            category="discord",
            event_type="dispatch_backend_unavailable",
            actor=f"discord:{request.author_name}",
            device_id=self.settings.device_id,
            project_id=project.project_id or project.channel_name,
            payload_json=json.dumps(
                {
                    "runtime": self.runtime,
                    "backend": self.backend,
                    "reason": self.reason,
                    "channel_id": request.channel_id,
                    "channel_name": request.channel_name,
                    "message_id": request.message_id,
                    "project_channel": project.channel_name,
                    "project_cwd": str(project.cwd),
                },
                sort_keys=True,
            ),
        )
        return result


class CodexDispatcher:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        runner: Runner | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        timeout_seconds: int = 900,
        codex_bin: str | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.popen_factory = popen_factory or subprocess.Popen
        self.timeout_seconds = timeout_seconds
        self.codex_bin = codex_bin or resolve_codex_bin()
        self.codex_sandbox = resolve_codex_sandbox()
        self.codex_model = resolve_codex_model()
        self.db = BemineDB(settings.db_path)

    def dispatch(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> DispatchResult:
        started = time.monotonic()
        if _dispatch_cancelled(request):
            return _interrupted_result(project, started)
        self._audit(
            "codex_dispatch_started",
            project,
            request,
            {"timeout_seconds": self.timeout_seconds},
        )
        prompt = build_codex_prompt(project, request)
        with tempfile.NamedTemporaryFile(
            "w+", encoding="utf-8", delete=True
        ) as output_file:
            command = [
                self.codex_bin,
                "exec",
                "--json",
                "--cd",
                str(project.cwd),
                "--sandbox",
                self.codex_sandbox,
                "--output-last-message",
                output_file.name,
                "-",
            ]
            if self.codex_model:
                command.extend(["--model", self.codex_model])
            for attachment in request.attachments:
                if _is_image_path(attachment):
                    command.extend(["--image", str(attachment)])
            if self.runner is not None:
                return self._dispatch_with_runner(
                    command=command,
                    prompt=prompt,
                    output_file=output_file,
                    started=started,
                    project=project,
                    request=request,
                    progress_callback=progress_callback,
                )
            try:
                proc = self.popen_factory(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                stdout = self._stream_process(
                    proc=proc,
                    prompt=prompt,
                    project=project,
                    request=request,
                    progress_callback=progress_callback,
                    started=started,
                )
            except DispatchInterrupted:
                elapsed = time.monotonic() - started
                result = DispatchResult(
                    ok=False,
                    project=project,
                    output="Codex dispatch interrupted by a newer Discord message.",
                    elapsed_seconds=elapsed,
                    returncode=None,
                    interrupted=True,
                )
                self._audit_result("codex_dispatch_finished", project, request, result)
                return result
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - started
                result = DispatchResult(
                    ok=False,
                    project=project,
                    output=(
                        f"Codex dispatch timed out after {self.timeout_seconds}s for "
                        f"`#{project.channel_name}`."
                    ),
                    elapsed_seconds=elapsed,
                    timed_out=True,
                )
                self._audit_result("codex_dispatch_finished", project, request, result)
                return result
            except FileNotFoundError as exc:
                elapsed = time.monotonic() - started
                result = DispatchResult(
                    ok=False,
                    project=project,
                    output=(
                        "Codex dispatch could not start because the `codex` binary was not found. "
                        "Set `BEMINE_CODEX_BIN` to the absolute Codex executable path."
                    ),
                    elapsed_seconds=elapsed,
                    returncode=None,
                )
                self._audit_result(
                    "codex_dispatch_finished",
                    project,
                    request,
                    result,
                    extra={"error": str(exc), "codex_bin": self.codex_bin},
                )
                return result
            except Exception as exc:
                elapsed = time.monotonic() - started
                result = DispatchResult(
                    ok=False,
                    project=project,
                    output=f"Codex dispatch failed before completion: {_redact(str(exc))}",
                    elapsed_seconds=elapsed,
                    returncode=None,
                )
                self._audit_result(
                    "codex_dispatch_finished",
                    project,
                    request,
                    result,
                    extra={
                        "error_type": type(exc).__name__,
                        "error": _redact(str(exc)),
                    },
                )
                return result

            output_file.seek(0)
            final_message = output_file.read().strip()
        output = final_message or stdout.strip()
        if not output:
            output = "Codex finished without a final message."
        output = _redact(output)
        elapsed = time.monotonic() - started
        returncode = proc.poll()
        result = DispatchResult(
            ok=returncode == 0,
            project=project,
            output=output,
            elapsed_seconds=elapsed,
            returncode=returncode,
        )
        self._audit_result("codex_dispatch_finished", project, request, result)
        return result

    def _dispatch_with_runner(
        self,
        *,
        command: list[str],
        prompt: str,
        output_file: Any,
        started: float,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None,
    ) -> DispatchResult:
        try:
            if _dispatch_cancelled(request):
                result = _interrupted_result(project, started)
                self._audit_result("codex_dispatch_finished", project, request, result)
                return result
            proc = self.runner(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
            if _dispatch_cancelled(request):
                result = _interrupted_result(project, started)
                self._audit_result("codex_dispatch_finished", project, request, result)
                return result
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=(
                    f"Codex dispatch timed out after {self.timeout_seconds}s for "
                    f"`#{project.channel_name}`."
                ),
                elapsed_seconds=elapsed,
                timed_out=True,
            )
            self._audit_result("codex_dispatch_finished", project, request, result)
            return result
        except FileNotFoundError as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=(
                    "Codex dispatch could not start because the `codex` binary was not found. "
                    "Set `BEMINE_CODEX_BIN` to the absolute Codex executable path."
                ),
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "codex_dispatch_finished",
                project,
                request,
                result,
                extra={"error": str(exc), "codex_bin": self.codex_bin},
            )
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            result = DispatchResult(
                ok=False,
                project=project,
                output=f"Codex dispatch failed before completion: {_redact(str(exc))}",
                elapsed_seconds=elapsed,
                returncode=None,
            )
            self._audit_result(
                "codex_dispatch_finished",
                project,
                request,
                result,
                extra={
                    "error_type": type(exc).__name__,
                    "error": _redact(str(exc)),
                },
            )
            return result

        self._handle_codex_stream_text(proc.stdout, project, request, progress_callback)
        output_file.seek(0)
        final_message = output_file.read().strip()
        output = final_message or proc.stdout.strip() or proc.stderr.strip()
        if not output:
            output = "Codex finished without a final message."
        output = _redact(output)
        elapsed = time.monotonic() - started
        result = DispatchResult(
            ok=proc.returncode == 0,
            project=project,
            output=output,
            elapsed_seconds=elapsed,
            returncode=proc.returncode,
        )
        self._audit_result("codex_dispatch_finished", project, request, result)
        return result

    def _stream_process(
        self,
        *,
        proc: subprocess.Popen[str],
        prompt: str,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None,
        started: float,
    ) -> str:
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("codex process did not expose stdin/stdout")
        proc.stdin.write(prompt)
        proc.stdin.close()

        lines: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        collected: list[str] = []
        deadline = started + self.timeout_seconds
        stream_finished = False
        while not stream_finished:
            if _dispatch_cancelled(request):
                _terminate_process(proc)
                raise DispatchInterrupted()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise subprocess.TimeoutExpired(
                    cmd=proc.args, timeout=self.timeout_seconds
                )
            try:
                line = lines.get(timeout=min(0.2, max(0.01, remaining)))
            except queue.Empty:
                if proc.poll() is not None and not reader.is_alive():
                    break
                continue
            if line is None:
                stream_finished = True
                continue
            collected.append(line)
            self._handle_codex_stream_text(line, project, request, progress_callback)
        reader.join(timeout=1)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        return "".join(collected)

    def _handle_codex_stream_text(
        self,
        text: str,
        project: ProjectChannel,
        request: DispatchRequest,
        progress_callback: ProgressCallback | None,
    ) -> None:
        for line in text.splitlines():
            event = _parse_json_line(line)
            if not event:
                continue
            self._record_codex_event(project, request, event)
            progress = codex_progress_from_event(event)
            if progress and progress_callback:
                progress_callback(progress)

    def _record_codex_event(
        self,
        project: ProjectChannel,
        request: DispatchRequest,
        event: Mapping[str, Any],
    ) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, Mapping):
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                cached_tokens = int(usage.get("cached_input_tokens") or 0)
                reasoning_tokens = int(usage.get("reasoning_output_tokens") or 0)
                self.db.record_usage_sample(
                    provider="codex",
                    account="codex",
                    runtime="codex",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    raw_json=json.dumps(event, sort_keys=True),
                )
                self._audit(
                    "codex_usage_recorded",
                    project,
                    request,
                    {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": reasoning_tokens,
                    },
                )
            return
        item = event.get("item")
        if event_type in {"item.started", "item.completed"} and isinstance(
            item, Mapping
        ):
            self._audit(
                "codex_tool_event",
                project,
                request,
                {
                    "codex_event_type": event_type,
                    "item_type": str(item.get("type") or ""),
                    "summary": codex_progress_summary(event) or "",
                },
            )

    def _audit(
        self,
        event_type: str,
        project: ProjectChannel,
        request: DispatchRequest,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "channel_id": request.channel_id,
            "channel_name": request.channel_name,
            "message_id": request.message_id,
            "project_channel": project.channel_name,
            "project_cwd": str(project.cwd),
            "runtime": project.runtime,
        }
        if extra:
            payload.update(extra)
        self.db.record_audit_event(
            category="discord",
            event_type=event_type,
            actor=f"discord:{request.author_name}",
            device_id=self.settings.device_id,
            payload_json=json.dumps(payload, sort_keys=True),
        )

    def _audit_result(
        self,
        event_type: str,
        project: ProjectChannel,
        request: DispatchRequest,
        result: DispatchResult,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "ok": result.ok,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "interrupted": result.interrupted,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
        }
        if extra:
            payload.update(extra)
        self._audit(event_type, project, request, payload)


def resolve_codex_bin() -> str:
    configured = os.environ.get("BEMINE_CODEX_BIN", "").strip()
    configured = configured or config_string("codex", "bin", "")
    if configured:
        return configured
    found = shutil.which("codex")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".npm-global" / "bin" / "codex",
        home / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/opt/homebrew/bin/codex"),
    ):
        if candidate.exists():
            return str(candidate)
    return "codex"


def resolve_codex_sandbox() -> str:
    configured = os.environ.get("BEMINE_CODEX_SANDBOX", "").strip()
    configured = configured or config_string("codex", "sandbox", "")
    if not configured:
        return "workspace-write"
    if configured not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(
            "BEMINE_CODEX_SANDBOX must be one of: read-only, workspace-write, danger-full-access"
        )
    return configured


def resolve_codex_model() -> str:
    return os.environ.get("BEMINE_CODEX_MODEL", "").strip() or config_string(
        "codex", "model", ""
    )


def resolve_codex_backend() -> str:
    configured = os.environ.get("BEMINE_CODEX_BACKEND", "").strip().lower()
    configured = configured or config_string("codex", "backend", "exec").lower()
    if configured in {"cli", "codex-cli"}:
        return "exec"
    if configured in {"server", "appserver", "app_server"}:
        return "app-server"
    return configured or "exec"


def resolve_codex_dispatcher(settings: BemineSettings) -> DispatchBackend:
    backend = resolve_codex_backend()
    if backend == "exec":
        return CodexDispatcher(settings)
    if backend == "app-server":
        return UnsupportedConfiguredDispatcher(
            settings,
            runtime="codex",
            backend=backend,
            reason=(
                "Codex app-server needs a persistent JSON-RPC thread adapter. "
                "`codex exec` is the implemented Codex backend today."
            ),
        )
    return UnsupportedConfiguredDispatcher(
        settings,
        runtime="codex",
        backend=backend,
        reason="unknown Codex backend; supported today: exec",
    )


def resolve_kimi_bin() -> str:
    configured = os.environ.get("BEMINE_KIMI_BIN", "").strip()
    configured = configured or config_string("kimi", "bin", "")
    if configured:
        return configured
    found = shutil.which("kimi")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "kimi",
        home / ".npm-global" / "bin" / "kimi",
        Path("/usr/local/bin/kimi"),
        Path("/opt/homebrew/bin/kimi"),
    ):
        if candidate.exists():
            return str(candidate)
    return "kimi"


def resolve_kimi_backend() -> str:
    configured = os.environ.get("BEMINE_KIMI_DISPATCH", "").strip().lower()
    configured = configured or os.environ.get("BEMINE_KIMI_BACKEND", "").strip().lower()
    configured = configured or config_string("kimi", "backend", "").lower()
    configured = configured or config_string("kimi", "dispatch", "native").lower()
    if configured in {"kimi-cli"}:
        return "cli"
    return configured or "native"


def resolve_kimi_dispatcher(settings: BemineSettings) -> DispatchBackend:
    backend = resolve_kimi_backend()
    if backend in {"cli", "kimi-cli"}:
        return KimiDispatcher(settings)
    if backend == "native":
        return KimiNativeDispatcher(settings)
    return UnsupportedConfiguredDispatcher(
        settings,
        runtime="kimi",
        backend=backend,
        reason="unknown Kimi backend; supported today: native, cli",
    )


def resolve_claude_backend() -> str:
    configured = os.environ.get("BEMINE_CLAUDE_BACKEND", "").strip().lower()
    configured = configured or config_string("claude", "backend", "meridian").lower()
    if configured in {"claude-code", "claude_cli", "claude-cli"}:
        return "claude-cli"
    return configured or "meridian"


def resolve_claude_model() -> str:
    return os.environ.get("BEMINE_CLAUDE_MODEL", "").strip() or config_string(
        "claude", "model", "claude-sonnet-4-6"
    )


def resolve_meridian_base_url() -> str:
    configured = os.environ.get("BEMINE_MERIDIAN_BASE_URL", "").strip()
    configured = configured or config_string("claude", "base_url", "")
    configured = configured or config_string("meridian", "base_url", "")
    return configured.rstrip("/") or "http://127.0.0.1:3456"


def resolve_meridian_api_key_env() -> str:
    configured = os.environ.get("BEMINE_MERIDIAN_API_KEY_ENV", "").strip()
    configured = configured or config_string("claude", "api_key_env", "")
    configured = configured or config_string("meridian", "api_key_env", "")
    return configured or "MERIDIAN_API_KEY"


def resolve_claude_dispatcher(settings: BemineSettings) -> DispatchBackend:
    backend = resolve_claude_backend()
    if backend == "meridian":
        return UnsupportedConfiguredDispatcher(
            settings,
            runtime="claude",
            backend=backend,
            reason=(
                "Meridian is an Anthropic/OpenAI-compatible HTTP proxy. "
                "Bemine still needs a Claude Anthropic-messages adapter that points "
                f"at `{resolve_meridian_base_url()}` and records tool calls like Kimi native dispatch."
            ),
        )
    return UnsupportedConfiguredDispatcher(
        settings,
        runtime="claude",
        backend=backend,
        reason="unknown Claude backend; supported target today: meridian",
    )


def native_session_id(channel_id: str, project_name: str) -> str:
    channel = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", channel_id.strip()) or "unknown"
    project = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", project_name.strip()) or "project"
    return f"discord:{channel}:{project}:kimi"


def render_kimi_cli_config(settings: BemineSettings, config: Any) -> Path:
    target = settings.state_dir / "runtime" / "kimi-config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    base_url = _kimi_cli_base_url(str(config.base_url))
    model = str(config.model or "kimi-k2.6")
    target.write_text(
        "\n".join(
            [
                'default_model = "bemine"',
                "default_thinking = false",
                "default_yolo = true",
                "",
                "[models.bemine]",
                'provider = "bemine-kimi"',
                f'model = "{_toml_escape(model)}"',
                "max_context_size = 200000",
                'capabilities = ["image_in", "thinking"]',
                "",
                "[providers.bemine-kimi]",
                'type = "kimi"',
                f'base_url = "{_toml_escape(base_url)}"',
                'api_key = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return target


def _kimi_cli_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/coding"):
        return f"{normalized}/v1"
    return normalized


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def resolve_dispatch_runtime(runtime: str | None = None) -> str:
    configured = (runtime or "").strip().lower()
    if not configured:
        configured = os.environ.get("BEMINE_DISPATCH_RUNTIME", "").strip().lower()
    if not configured:
        configured = config_string("dispatch", "runtime", "").lower()
    return configured or "kimi"


# Real default model ids per runtime, matching bemine.toml's configured values.
# Used when [<runtime>].model is unset so a blank config never leaks the runtime
# name (or device id) to the SDK as a bogus model id (-> 404).
_DEFAULT_MODELS = {
    "kimi": "kimi-k2.6",
    "claude": "claude-sonnet-4-6",
    "codex": "gpt-5.3-codex-spark",
}


@dataclass(frozen=True)
class ChannelBinding:
    project_id: str
    runtime: str
    model: str
    cwd: Path
    system: str


def _shell_capability_lines() -> list[str]:
    policy = config_string("policy", "shell", "project-write")
    if policy == "unrestricted":
        return [
            "- You have a GENERAL system shell via `shell_run`: you can run any command "
            "anywhere on this machine, install software, manage services, and call external "
            "APIs (including Discord using $MAYOR_DISCORD_BOT_TOKEN from your environment).",
            "- Default to DOING the task with the shell rather than saying you cannot. To act "
            "on Discord (e.g. create a channel), call the Discord REST API with the bot token; "
            "discover the target guild via GET /users/@me/guilds if you don't know its id.",
            "- Pause to confirm only for clearly destructive or irreversible actions (deleting "
            "data, force-pushing, tearing down infrastructure).",
        ]
    return [
        "- Work inside the project root; `shell_run` is limited to the project directory.",
    ]


# Guild channels whose name is NOT a project (the session-watcher catch-all and the
# usual chit-chat channels). Every OTHER named guild channel is treated as a per-project
# channel, since Bemine auto-creates one channel per project (named after the project).
_NON_PROJECT_CHANNELS = {"general", "session-updates", "bemine", "mayor"}


def _non_project_channel_names() -> set[str]:
    names = {_channel_key(n) for n in _NON_PROJECT_CHANNELS}
    fallback = config_string("jobs", "session_fallback_channel", "session-updates")
    if fallback:
        names.add(_channel_key(fallback))
    return names


def _project_channel_block(project: ProjectChannel, *, local: bool) -> list[str]:
    """Strong, deterministic instructions so the agent always knows this channel IS a
    specific project and never asks the user which project/session they mean."""
    name = project.channel_name or project.project_name
    block = [
        "",
        f"CHANNEL BINDING (authoritative): this Discord channel is dedicated to the "
        f"project `{name}`.",
        f"- Every message in this channel is about `{name}`. Never ask the user which "
        "project or which session they mean — it is always this one.",
        f'- A bare "status", "it", "its", or "the session" refers to the `{name}` '
        "project's agent session, NOT your own runtime/model/config. Only call "
        "`bemine_config` (and report your own runtime/model) when the user explicitly "
        "asks what model or runtime YOU are.",
        f"- To answer about `{name}` or act on it, first call `status_show` and pick the "
        f"session whose working directory's last path segment is `{name}` (it may be "
        "running on another device). That session is what this channel controls; report "
        "its device, last activity, and whether it is waiting for input.",
    ]
    if local:
        block.append(
            f"- To relay a message into a waiting local session for `{name}`, use "
            "`tmux_list` then `tmux_send` to the pane whose path matches it."
        )
    else:
        block.append(
            f"- `{name}` has no checkout on this device; it runs on the device shown by "
            "`status_show`. To relay a message/command into its session, use your shell "
            "to reach that device (e.g. ssh + tmux send-keys). If you cannot reach it, "
            "say exactly what is blocking instead of asking which session."
        )
    return block


def _subagent_prompt_context() -> str:
    return load_subagent_context().text or "No subagent profiles configured."


def _build_channel_system_prompt(
    settings: BemineSettings,
    project: ProjectChannel | None,
    *,
    runtime: str = "",
    model: str = "",
    local_project: bool = True,
) -> str:
    """Faithful reuse of the existing native-worker system prompt content. Concatenates
    the redacted harness + operator context the same way build_kimi_native_system_prompt
    does, without inventing new prompt policy."""
    harness_context = load_harness_context().text or "No harness context files found."
    operator_context = (
        load_operator_context().text or "No operator context files found."
    )
    subagent_context = _subagent_prompt_context()
    project_line = (
        f"Project root: {project.cwd}"
        if project
        else "Project root: (none bound; coordination/default surface)"
    )
    project_state_block: list[str] = []
    if project is not None and local_project:
        from bemine.context.project_state import project_state_context

        try:
            state_text = project_state_context(
                settings,
                project_id=project.project_id or project.channel_name,
                cwd=Path(project.cwd),
            )
        except Exception:
            state_text = ""
        if state_text:
            project_state_block = ["", "Current project state:", state_text]
    identity = [
        "Active runtime configuration (AUTHORITATIVE — this is the ground truth; if the "
        "user asks which model/runtime/configuration you are, answer with exactly this and "
        "never guess a different one):",
        f"- runtime: {runtime or 'unknown'}",
        f"- model: {model or 'unknown'}",
        f"- device: {settings.device_id}",
        f"You are running as the `{runtime or 'unknown'}` runtime using model "
        f"`{model or 'unknown'}`.",
        "",
    ]
    channel_block = (
        _project_channel_block(project, local=local_project) if project else []
    )
    return _redact(
        "\n".join(
            [
                "You are Bemine's native worker for a Discord-routed task.",
                "You run inside Bemine's own harness and use only the tools Bemine provides.",
                project_line,
                *channel_block,
                *project_state_block,
                "",
                *identity,
                "",
                "Bemine harness context:",
                harness_context,
                "",
                "Shared operator context:",
                operator_context,
                "",
                "Configured subagent profiles:",
                subagent_context,
                "",
                "Credential and infrastructure rules:",
                "- Use status/capability tools before assuming credential access.",
                "- Hugging Face identity/token access is baseline shared when present; never print the token.",
                "- AWS, Google, Porkbun, and other cost-incurring or infrastructure credentials require explicit permission.",
                "- Deployment runbooks live in operator context, especially `~/deploy.md`.",
                "",
                "Runtime-specific rules:",
                "- You are an OPERATOR, not an advisor: NEVER reply with commands for the "
                "user to run — run them yourself with your tools and report the results. "
                "Your full tool manual is AGENTS.md (in the harness context above).",
                "- Use Bemine tool calls when local state, files, commands, status, or audit history are needed.",
                "- Do not claim a command ran unless a tool result shows it ran.",
                *_shell_capability_lines(),
                "- Keep final responses concise and suitable for Discord Markdown.",
                "- If you changed files, mention the files and verification command.",
                "- If blocked or needing approval/credentials, say exactly what is needed.",
                "- Never print or expose secrets.",
            ]
        )
    )


def resolve_channel_binding(settings: BemineSettings, message: Any) -> ChannelBinding:
    """Deterministic: channel/category -> project (resolve_project_channel); runtime/model
    from config; NO content inference. DMs and unmapped channels fall back to a default
    project + default runtime so a novel message still reaches a real agent."""
    channel = getattr(message, "channel", None)
    channel_name = getattr(channel, "name", "") or ""
    category = getattr(channel, "category", None)
    category_name = getattr(category, "name", "") if category else ""
    is_dm = getattr(message, "guild", None) is None
    project = resolve_project_channel(
        channel_name=channel_name,
        category_name=category_name,
        state_dir=settings.state_dir,
        repo_dir=None,
    )
    # Bemine auto-creates one channel per project (named after the project) with NO
    # Discord category, so resolve_project_channel — which gates on the project category —
    # misses them. Bind the channel by NAME instead so the agent always knows which
    # project/session it controls, instead of falling back to the generic coordination
    # surface and asking the user which session they mean.
    local_project = project is not None
    if (
        project is None
        and not is_dm
        and channel_name
        and _channel_key(channel_name) not in _non_project_channel_names()
    ):
        # A project with a local checkout (config or defaults) -> real cwd, local tools.
        local_match = load_project_channels(settings.state_dir).get(
            _channel_key(channel_name)
        )
        if local_match is not None:
            project = local_match
            local_project = True
        else:
            # Otherwise it lives on another device: bind a remote project identity.
            project = ProjectChannel(
                channel_name=channel_name,
                cwd=Path(settings.state_dir).parent,
                project_id=f"channel:{_channel_key(channel_name)}",
                project_name=channel_name,
            )
    runtime = resolve_dispatch_runtime(project.runtime if project else None)
    model = config_string(runtime, "model", "") or _DEFAULT_MODELS.get(runtime, "")
    cwd = Path(project.cwd) if local_project else Path(settings.state_dir).parent
    project_id = (project.project_id or project.channel_name) if project else "bemine"
    system = _build_channel_system_prompt(
        settings, project, runtime=runtime, model=model, local_project=local_project
    )
    return ChannelBinding(
        project_id=project_id,
        runtime=runtime,
        model=model,
        cwd=cwd,
        system=system,
    )


def dispatch_status_payload(settings: BemineSettings) -> dict[str, Any]:
    runtime = resolve_dispatch_runtime()
    payload: dict[str, Any] = {
        "runtime": runtime,
        "runtime_source": "BEMINE_DISPATCH_RUNTIME"
        if os.environ.get("BEMINE_DISPATCH_RUNTIME", "").strip()
        else "bemine.toml"
        if config_string("dispatch", "runtime", "")
        else "default",
    }
    if runtime == "kimi":
        config = KimiHandler(settings.hermes_home).status()
        backend = resolve_kimi_backend()
        payload.update(
            {
                "provider": config.provider,
                "model": config.model,
                "api_mode": config.api_mode,
                "base_url": config.base_url,
                "api_key_present": config.api_key_present,
                "model_source": str(config_path())
                if config_string("kimi", "model", "")
                else config.config_path,
                "backend": backend,
                "kimi_bin": resolve_kimi_bin(),
                "tool_calls": "available through Bemine native tools"
                if backend != "cli"
                else "available through kimi --print --yolo CLI tools",
            }
        )
    elif runtime == "codex":
        model = resolve_codex_model()
        backend = resolve_codex_backend()
        payload.update(
            {
                "provider": "codex",
                "model": model or "codex config/default",
                "model_source": "BEMINE_CODEX_MODEL"
                if os.environ.get("BEMINE_CODEX_MODEL", "").strip()
                else "bemine.toml"
                if model
                else "$CODEX_HOME/config.toml",
                "backend": backend,
                "codex_bin": resolve_codex_bin(),
                "sandbox": resolve_codex_sandbox(),
                "tool_calls": "available through codex exec --json progress events"
                if backend == "exec"
                else "requires Codex app-server adapter",
            }
        )
    elif runtime == "claude":
        backend = resolve_claude_backend()
        api_key_env = resolve_meridian_api_key_env() if backend == "meridian" else ""
        payload.update(
            {
                "provider": "claude",
                "model": resolve_claude_model(),
                "model_source": "BEMINE_CLAUDE_MODEL"
                if os.environ.get("BEMINE_CLAUDE_MODEL", "").strip()
                else "bemine.toml",
                "backend": backend,
                "base_url": resolve_meridian_base_url()
                if backend == "meridian"
                else "",
                "api_key_env": api_key_env,
                "api_key_present": bool(os.environ.get(api_key_env, "").strip())
                if api_key_env
                else None,
                "tool_calls": "requires Meridian Anthropic-messages adapter"
                if backend == "meridian"
                else "unsupported Claude backend",
            }
        )
    else:
        payload.update({"tool_calls": "unsupported runtime"})
    return payload


def render_dispatch_status(payload: Mapping[str, Any]) -> str:
    lines = [
        "Dispatch runtime:",
        f"- runtime: {payload.get('runtime')}",
        f"- model: {payload.get('model', 'unknown')}",
        f"- provider: {payload.get('provider', 'unknown')}",
        f"- model_source: {payload.get('model_source', 'unknown')}",
        f"- tool_calls: {payload.get('tool_calls', 'unknown')}",
    ]
    if payload.get("backend"):
        lines.append(f"- backend: {payload.get('backend')}")
    if payload.get("sandbox"):
        lines.append(f"- sandbox: {payload.get('sandbox')}")
    if payload.get("api_key_present") is not None:
        present = "present" if payload.get("api_key_present") else "missing"
        lines.append(f"- api_key: {present}")
    if payload.get("base_url"):
        lines.append(f"- base_url: {payload.get('base_url')}")
    return "\n".join(lines) + "\n"


def codex_progress_from_event(event: Mapping[str, Any]) -> DispatchProgress | None:
    summary = codex_progress_summary(event)
    if not summary:
        return None
    event_type = str(event.get("type") or "")
    return DispatchProgress(
        runtime="codex",
        event_type=event_type,
        summary=summary,
        raw_event=event,
    )


def codex_progress_summary(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    item = event.get("item")
    if not isinstance(item, Mapping):
        return ""
    item_type = str(item.get("type") or "")
    if item_type != "command_execution":
        return ""
    command = _compact_progress_value(str(item.get("command") or "shell command"))
    if event_type == "item.started":
        return f"💻 shell: `{command}`"
    if event_type == "item.completed":
        exit_code = item.get("exit_code")
        status = "done" if exit_code == 0 else f"exit {exit_code}"
        return (
            f"✅ shell {status}: `{command}`"
            if exit_code == 0
            else f"⚠️ shell {status}: `{command}`"
        )
    return ""


def build_codex_prompt(project: ProjectChannel, request: DispatchRequest) -> str:
    return _build_dispatch_prompt(
        project,
        request,
        runtime_name="Codex",
        runtime_notes=[
            "- You may inspect and edit files using Codex tools as needed.",
        ],
    )


def build_kimi_prompt(project: ProjectChannel, request: DispatchRequest) -> str:
    return _build_dispatch_prompt(
        project,
        request,
        runtime_name="Kimi",
        runtime_notes=[
            "- You may inspect and edit files using Kimi CLI tools as needed.",
            "- Use shell/file tools directly when the task requires local action.",
        ],
    )


def build_kimi_native_prompt(project: ProjectChannel, request: DispatchRequest) -> str:
    attachment_lines = [f"- {path}" for path in request.attachments] or ["- none"]
    return "\n".join(
        [
            f"Route reason: {request.route_reason or 'unspecified'}",
            f"Source channel: #{request.channel_name}",
            f"Project channel: #{project.channel_name}",
            f"Project id: {project.project_id or '(unknown)'}",
            f"Project name: {project.project_name or project.channel_name}",
            f"Project root: {project.cwd}",
            f"Discord message id: {request.message_id}",
            f"User: {request.author_name}",
            "",
            "Recent channel context:",
            request.conversation_context.strip() or "- none",
            "",
            "User message:",
            request.content.strip() or "(no text; inspect attachments if useful)",
            "",
            "Saved attachments:",
            *attachment_lines,
        ]
    )


def build_kimi_native_system_prompt(
    project: ProjectChannel,
    request: DispatchRequest,
) -> str:
    del project, request
    harness_context = load_harness_context().text or "No harness context files found."
    operator_context = (
        load_operator_context().text or "No operator context files found."
    )
    subagent_context = _subagent_prompt_context()
    return "\n".join(
        [
            "You are Bemine's native Kimi worker for a Discord-routed task.",
            "You run inside Bemine's own harness and use only the tools Bemine provides.",
            "",
            "Bemine harness context:",
            harness_context,
            "",
            "Shared operator context:",
            operator_context,
            "",
            "Configured subagent profiles:",
            subagent_context,
            "",
            "Credential and infrastructure rules:",
            "- Use status/capability tools before assuming credential access.",
            "- Hugging Face identity/token access is baseline shared when present; never print the token.",
            "- AWS, Google, Porkbun, and other cost-incurring or infrastructure credentials require explicit permission.",
            "- Deployment runbooks live in operator context, especially `~/deploy.md`.",
            "",
            "Runtime-specific rules:",
            "- Use Bemine tool calls when local state, files, commands, status, or audit history are needed.",
            "- Do not claim a command ran unless a tool result shows it ran.",
            "- Work inside the project root unless the user clearly asks for coordination only.",
            "- Keep final responses concise and suitable for Discord Markdown.",
            "- If you changed files, mention the files and verification command.",
            "- If blocked or needing approval/credentials, say exactly what is needed.",
            "- Never print or expose secrets.",
        ]
    )


def _build_dispatch_prompt(
    project: ProjectChannel,
    request: DispatchRequest,
    *,
    runtime_name: str,
    runtime_notes: list[str],
) -> str:
    attachment_lines = [f"- {path}" for path in request.attachments] or ["- none"]
    harness_context = load_harness_context().text or "No harness context files found."
    operator_context = (
        load_operator_context().text or "No operator context files found."
    )
    subagent_context = _subagent_prompt_context()
    return "\n".join(
        [
            f"You are Bemine's {runtime_name} worker for a Discord-routed task.",
            f"Route reason: {request.route_reason or 'unspecified'}",
            f"Source channel: #{request.channel_name}",
            f"Project channel: #{project.channel_name}",
            f"Project id: {project.project_id or '(unknown)'}",
            f"Project name: {project.project_name or project.channel_name}",
            f"Project root: {project.cwd}",
            f"Discord message id: {request.message_id}",
            f"User: {request.author_name}",
            "",
            "Recent channel context:",
            request.conversation_context.strip() or "- none",
            "",
            "User message:",
            request.content.strip() or "(no text; inspect attachments if useful)",
            "",
            "Saved attachments:",
            *attachment_lines,
            "",
            "Bemine harness context:",
            harness_context,
            "",
            "Shared operator context:",
            operator_context,
            "",
            "Configured subagent profiles:",
            subagent_context,
            "",
            "Credential and infrastructure rules:",
            "- Use `bemine capabilities` before assuming credential access.",
            "- Hugging Face identity/token access is baseline shared when present; never print the token.",
            "- AWS, Google, Porkbun, and other cost-incurring or infrastructure credentials require explicit permission.",
            '- If a task needs gated access, ask through `bemine permissions ask <credential_id> --reason "<why>"`.',
            "- Deployment runbooks live in operator context, especially `~/deploy.md`.",
            "",
            "Runtime-specific rules:",
            *runtime_notes,
            "",
            "Operational rules:",
            "- Work inside the project root unless the user clearly asks for coordination only.",
            "- Keep the final response concise and suitable for Discord.",
            "- If you changed files, mention the files and verification command.",
            "- If blocked or needing approval/credentials, say exactly what is needed.",
            "- Never print or expose secrets.",
        ]
    )


def _parse_json_line(line: str) -> Mapping[str, Any] | None:
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _compact_progress_value(value: str, *, limit: int = 120) -> str:
    text = _redact(" ".join(value.split()))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _default_project_channels(
    *, repo_dir: Path, home: Path
) -> dict[str, ProjectChannel]:
    candidates = {
        "tts": home / "github" / "streaming-tts",
        "streaming-tts": home / "github" / "streaming-tts",
        "neptts-bench": home / "github" / "neptts-bench",
        "bemine": repo_dir,
    }
    result: dict[str, ProjectChannel] = {}
    for channel_name, cwd in candidates.items():
        if cwd.exists():
            channel = ProjectChannel(
                channel_name=channel_name,
                cwd=cwd.resolve(),
                project_name=channel_name,
            )
            result[_channel_key(channel_name)] = channel
    return result


def _project_channel_from_config(name: str, value: Any) -> ProjectChannel:
    if isinstance(value, str):
        return ProjectChannel(
            channel_name=name,
            cwd=Path(value).expanduser().resolve(),
        )
    if not isinstance(value, dict):
        raise ValueError(f"project channel {name!r} must be a string or object")
    cwd = Path(str(value["cwd"])).expanduser().resolve()
    return ProjectChannel(
        channel_name=str(value.get("channel_name") or name),
        cwd=cwd,
        project_id=str(value.get("project_id") or ""),
        project_name=str(value.get("project_name") or value.get("name") or name),
    )


def _channel_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _is_project_category_name(value: str) -> bool:
    key = _channel_key(value)
    base = _channel_key(PROJECT_CATEGORY_NAME)
    if key == base:
        return True
    for separator in (" ", "-"):
        suffix = key.removeprefix(f"{base}{separator}")
        if suffix != key:
            return suffix.isdecimal() and bool(suffix)
    return False


def _is_image_path(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _project_from_content(
    content: str,
    channels: Mapping[str, ProjectChannel],
) -> ProjectChannel | None:
    text = _content_key(content)
    if not text:
        return None
    ordered = sorted(
        channels.values(), key=lambda channel: len(channel.channel_name), reverse=True
    )
    for channel in ordered:
        for variant in _project_variants(channel):
            if _content_mentions_project(text, variant):
                return channel
    return None


def _project_variants(channel: ProjectChannel) -> set[str]:
    raw_values = {channel.channel_name, channel.project_name}
    variants: set[str] = set()
    for raw in raw_values:
        if not raw:
            continue
        normalized = raw.strip().lower()
        variants.add(normalized)
        variants.add(normalized.replace("_", "-"))
        variants.add(normalized.replace("_", " "))
        variants.add(normalized.replace("-", " "))
    return {_content_key(variant) for variant in variants if _content_key(variant)}


def _content_key(value: str) -> str:
    text = value.lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9#:\\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _content_mentions_project(text: str, project_key: str) -> bool:
    escaped = re.escape(project_key)
    return any(
        re.search(pattern, text)
        for pattern in (
            rf"(^|\s)#{escaped}(\s|:|$)",
            rf"(^|\s){escaped}:(\s|$)",
            rf"(^|\s)(?:project|for|in|on)\s+{escaped}(\s|:|$)",
        )
    )


def _project_channels_from_rows(rows: list[Mapping[str, Any]]) -> list[ProjectChannel]:
    name_counts: dict[str, int] = {}
    for row in rows:
        name_counts[_slug(str(row["name"]))] = (
            name_counts.get(_slug(str(row["name"])), 0) + 1
        )

    used: set[str] = set()
    channels: list[ProjectChannel] = []
    for row in rows:
        project_id = str(row["project_id"])
        project_name = str(row["name"])
        cwd = Path(str(row["cwd"])).expanduser().resolve()
        if project_name == "bm" and cwd.name == "bm":
            channel_name = _deduped_channel_name("bemine", project_id, used)
        else:
            channel_name = _project_channel_name(
                project_name=project_name,
                cwd=cwd,
                project_id=project_id,
                duplicate_name=name_counts.get(_slug(project_name), 0) > 1,
                used=used,
            )
        channels.append(
            ProjectChannel(
                channel_name=channel_name,
                cwd=cwd,
                project_id=project_id,
                project_name=project_name,
            )
        )
    return channels


def _project_channel_name(
    *,
    project_name: str,
    cwd: Path,
    project_id: str,
    duplicate_name: bool,
    used: set[str],
) -> str:
    base = _slug(project_name)
    if duplicate_name:
        context = _path_context(cwd)
        if context:
            base = _slug(f"{base}-{context}")
    return _deduped_channel_name(base[:90].strip("-") or "project", project_id, used)


def _deduped_channel_name(base: str, project_id: str, used: set[str]) -> str:
    candidate = base
    if candidate in used:
        suffix = project_id.removeprefix("proj_")[:6]
        candidate = f"{candidate[: 93 - len(suffix)]}-{suffix}".strip("-")
    counter = 2
    original = candidate
    while candidate in used:
        suffix = f"-{counter}"
        candidate = f"{original[: 100 - len(suffix)]}{suffix}".strip("-")
        counter += 1
    used.add(candidate)
    return candidate


def _path_context(cwd: Path) -> str:
    parts = [
        part
        for part in cwd.parts
        if part not in {"", "/", "home", "cdjk", "crew", "apps", ".worktrees"}
    ]
    if len(parts) <= 1:
        return ""
    stem = _slug(parts[-1])
    context: list[str] = []
    for part in reversed(parts[:-1]):
        slug = _slug(part)
        if not slug or slug == stem or slug in context:
            continue
        context.append(slug)
        if len(context) >= 2:
            break
    return "-".join(reversed(context))


def _slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "project"


def _project_channel_payload(channel: ProjectChannel) -> dict[str, str]:
    payload = {
        "channel_name": channel.channel_name,
        "cwd": str(channel.cwd),
    }
    if channel.project_id:
        payload["project_id"] = channel.project_id
    if channel.project_name:
        payload["project_name"] = channel.project_name
    return payload


def _desired_project_channels(
    db: BemineDB,
    *,
    limit: int,
) -> tuple[list[ProjectChannel], dict[str, ProjectChannel]]:
    channels = project_channels_from_db(db, limit=limit)
    return channels, _project_aliases(channels)


def _desired_channel_entries(
    channels: list[ProjectChannel],
    aliases: Mapping[str, ProjectChannel],
) -> list[dict[str, str]]:
    entries = [
        {**_project_channel_payload(channel), "kind": "project"} for channel in channels
    ]
    for alias_name, channel in sorted(aliases.items()):
        entries.append(
            {
                **_project_channel_payload(
                    ProjectChannel(
                        channel_name=alias_name,
                        cwd=channel.cwd,
                        project_id=channel.project_id,
                        project_name=channel.project_name,
                        runtime=channel.runtime,
                    )
                ),
                "kind": "alias",
                "alias_for": channel.channel_name,
            }
        )
    return sorted(entries, key=lambda row: row["channel_name"])


def _cached_channel_entry(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        "channel_id": str(row.get("channel_id") or ""),
        "channel_name": str(row.get("channel_name") or ""),
        "category_name": str(row.get("category_name") or ""),
        "guild_id": str(row.get("guild_id") or ""),
    }


def _project_aliases(channels: list[ProjectChannel]) -> dict[str, ProjectChannel]:
    aliases: dict[str, ProjectChannel] = {}
    for channel in channels:
        if channel.project_name in {"streaming-tts", "neptts_bench", "neptts-bench"}:
            if channel.project_name == "streaming-tts":
                aliases["tts"] = channel
            elif channel.project_name in {"neptts_bench", "neptts-bench"}:
                aliases["neptts-bench"] = channel
    return aliases


async def _ensure_project_categories(
    session: aiohttp.ClientSession,
    guild_id: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    channels = await _discord_json(
        session, "GET", f"https://discord.com/api/v10/guilds/{guild_id}/channels"
    )
    if not isinstance(channels, list):
        raise RuntimeError("Discord channel list response was not a list")
    categories = _sort_project_categories(
        [
            channel
            for channel in channels
            if channel.get("type") == 4
            and _is_project_category_name(str(channel.get("name") or ""))
        ]
    )
    if categories:
        return categories, channels
    category = await _create_project_category(session, guild_id, PROJECT_CATEGORY_NAME)
    return [category], channels


async def _project_category_with_capacity(
    session: aiohttp.ClientSession,
    guild_id: str,
    categories: list[Mapping[str, Any]],
    category_counts: dict[str, int],
) -> Mapping[str, Any]:
    for category in categories:
        category_id = str(category.get("id") or "")
        if category_counts.get(category_id, 0) < MAX_CHANNELS_PER_CATEGORY:
            return category
    category_name = _next_project_category_name(categories)
    category = await _create_project_category(session, guild_id, category_name)
    categories.append(category)
    category_counts[str(category.get("id") or "")] = 0
    categories.sort(key=_project_category_sort_key)
    return category


async def _create_project_category(
    session: aiohttp.ClientSession,
    guild_id: str,
    name: str,
) -> Mapping[str, Any]:
    return await _discord_json(
        session,
        "POST",
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        json_payload={"name": name, "type": 4},
    )


def _sort_project_categories(
    categories: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return sorted(categories, key=_project_category_sort_key)


def _project_category_sort_key(category: Mapping[str, Any]) -> tuple[int, str]:
    name = str(category.get("name") or "")
    key = _channel_key(name)
    base = _channel_key(PROJECT_CATEGORY_NAME)
    if key == base:
        return (1, name)
    for separator in (" ", "-"):
        suffix = key.removeprefix(f"{base}{separator}")
        if suffix != key and suffix.isdecimal():
            return (int(suffix), name)
    return (999, name)


def _next_project_category_name(categories: list[Mapping[str, Any]]) -> str:
    used = {_channel_key(str(category.get("name") or "")) for category in categories}
    number = 2
    while _channel_key(f"{PROJECT_CATEGORY_NAME} {number}") in used:
        number += 1
    return f"{PROJECT_CATEGORY_NAME} {number}"


async def _create_text_channel(
    session: aiohttp.ClientSession,
    guild_id: str,
    name: str,
    *,
    parent_id: str,
    topic: str = "",
) -> Mapping[str, Any]:
    payload: dict[str, Any] = {"name": name, "type": 0, "parent_id": parent_id}
    if topic:
        payload["topic"] = topic
    return await _discord_json(
        session,
        "POST",
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        json_payload=payload,
    )


def _category_counts(
    categories: list[Mapping[str, Any]],
    channels: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    category_counts = {str(category.get("id") or ""): 0 for category in categories}
    for channel in channels:
        parent_id = str(channel.get("parent_id") or "")
        if parent_id in category_counts:
            category_counts[parent_id] += 1
    return category_counts


async def _delete_empty_overflow_categories(
    session: aiohttp.ClientSession,
    categories: list[Mapping[str, Any]],
    category_counts: dict[str, int],
) -> list[dict[str, str]]:
    deleted: list[dict[str, str]] = []
    for category in list(categories):
        category_name = str(category.get("name") or "")
        category_id = str(category.get("id") or "")
        if _channel_key(category_name) == _channel_key(PROJECT_CATEGORY_NAME):
            continue
        if category_counts.get(category_id, 0) != 0:
            continue
        await _delete_discord_channel(session, category_id)
        deleted.append({"id": category_id, "name": category_name})
    return deleted


async def _delete_discord_channel(
    session: aiohttp.ClientSession,
    channel_id: str,
) -> Mapping[str, Any]:
    return await _discord_json(
        session,
        "DELETE",
        f"https://discord.com/api/v10/channels/{channel_id}",
    )


async def _discord_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    json_payload: Mapping[str, Any] | None = None,
) -> Any:
    async with session.request(
        method, url, json=dict(json_payload) if json_payload else None
    ) as response:
        text = await response.text()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"raw": text}
        if response.status >= 400:
            raise RuntimeError(
                f"Discord API {method} {url} failed {response.status}: {data}"
            )
        return data
