from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.project_filters import ignored_project_reason
from bemine.status import _redact

PROJECT_CATEGORY_NAME = "Bemine Projects"
DISPATCH_CONFIG_NAME = "discord-projects.json"
MAX_CHANNELS_PER_CATEGORY = 50

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ProjectChannel:
    channel_name: str
    cwd: Path
    project_id: str = ""
    project_name: str = ""
    runtime: str = "codex"


@dataclass(frozen=True)
class DispatchRequest:
    channel_id: str
    channel_name: str
    message_id: str
    author_name: str
    content: str
    attachments: list[Path]
    route_reason: str = ""


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    project: ProjectChannel
    output: str
    elapsed_seconds: float
    returncode: int | None = None
    timed_out: bool = False


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
            if channel.cwd.exists() and not ignored_project_reason(channel.cwd, name=channel.project_name):
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
            runtime=channel.runtime,
        )
        payload[alias.channel_name] = _project_channel_payload(alias)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    return load_project_channels(state_dir, repo_dir=repo_dir).get(_channel_key(channel_name))


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
            return ProjectRoute(project=mentioned_project, reason=f"mentioned-channel:{mentioned_name}")

    channels = load_project_channels(state_dir, repo_dir=repo_dir)
    channel_match = channels.get(_channel_key(channel_name))
    if channel_match:
        return ProjectRoute(project=channel_match, reason=f"channel-name:{channel_name}")

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
        channel.channel_name: {"cwd": str(channel.cwd), "runtime": channel.runtime}
        for channel in defaults.values()
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


async def sync_project_channels_to_discord(
    settings: BemineSettings,
    discord_token: str,
    guild_id: str,
    *,
    limit: int = 200,
    prune: bool = False,
) -> dict[str, Any]:
    db = BemineDB(settings.db_path)
    channels = project_channels_from_db(db, limit=limit)
    aliases = _project_aliases(channels)
    config_path = write_project_channel_config(
        settings.state_dir,
        channels,
        aliases=aliases,
        preserve_existing=False,
    )
    target_names = {channel.channel_name for channel in channels}
    target_names.update(aliases.keys())

    headers = {
        "Authorization": f"Bot {discord_token}",
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": "Bemine per-project channel sync",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        categories, existing = await _ensure_project_categories(session, guild_id)
        project_category_ids = {str(category.get("id") or "") for category in categories}
        existing_project_channels = [
            channel
            for channel in existing
            if channel.get("type") == 0
            and str(channel.get("parent_id") or "") in project_category_ids
        ]
        existing_by_name = {str(channel.get("name")): channel for channel in existing_project_channels}
        deleted: list[dict[str, str]] = []
        if prune:
            for name, channel in list(existing_by_name.items()):
                if name in target_names:
                    continue
                await _delete_discord_channel(session, str(channel.get("id") or ""))
                deleted.append({"id": str(channel.get("id") or ""), "name": name})
                existing_by_name.pop(name, None)
        category_counts = _category_counts(categories, existing_by_name.values())
        created: list[dict[str, str]] = []
        skipped: list[str] = []
        for name in sorted(target_names):
            if name in existing_by_name:
                skipped.append(name)
                continue
            category = await _project_category_with_capacity(
                session,
                guild_id,
                categories,
                category_counts,
            )
            channel = await _create_text_channel(session, guild_id, name, parent_id=str(category["id"]))
            category_id = str(category.get("id") or "")
            category_counts[category_id] = category_counts.get(category_id, 0) + 1
            created.append(
                {
                    "id": str(channel.get("id") or ""),
                    "name": name,
                    "category_id": category_id,
                    "category_name": str(category.get("name") or ""),
                }
            )
        deleted_categories: list[dict[str, str]] = []
        if prune:
            deleted_categories = await _delete_empty_overflow_categories(
                session,
                categories,
                category_counts,
            )

    db.record_audit_event(
        category="discord",
        event_type="project_channels_synced",
        actor="channel_sync",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {
                "guild_id": guild_id,
                "category_ids": [str(category.get("id") or "") for category in categories],
                "created": created,
                "deleted": deleted,
                "deleted_categories": deleted_categories,
                "skipped_count": len(skipped),
                "config_path": str(config_path),
                "prune": prune,
            },
            sort_keys=True,
        ),
    )
    return {
        "guild_id": guild_id,
        "category_ids": [str(category.get("id") or "") for category in categories],
        "category_names": [str(category.get("name") or PROJECT_CATEGORY_NAME) for category in categories],
        "created": created,
        "deleted": deleted,
        "deleted_categories": deleted_categories,
        "skipped": skipped,
        "config_path": str(config_path),
        "project_count": len(channels),
    }


class CodexDispatcher:
    def __init__(
        self,
        settings: BemineSettings,
        *,
        runner: Runner | None = None,
        timeout_seconds: int = 900,
        codex_bin: str | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or subprocess.run
        self.timeout_seconds = timeout_seconds
        self.codex_bin = codex_bin or resolve_codex_bin()
        self.db = BemineDB(settings.db_path)

    def dispatch(self, project: ProjectChannel, request: DispatchRequest) -> DispatchResult:
        started = time.monotonic()
        self._audit("codex_dispatch_started", project, request, {"timeout_seconds": self.timeout_seconds})
        prompt = build_codex_prompt(project, request)
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=True) as output_file:
            command = [
                self.codex_bin,
                "exec",
                "--cd",
                str(project.cwd),
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--output-last-message",
                output_file.name,
                "-",
            ]
            for attachment in request.attachments:
                if _is_image_path(attachment):
                    command.extend(["--image", str(attachment)])
            try:
                proc = self.runner(
                    command,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                )
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
                    extra={"error_type": type(exc).__name__, "error": _redact(str(exc))},
                )
                return result

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
            "elapsed_seconds": round(result.elapsed_seconds, 3),
        }
        if extra:
            payload.update(extra)
        self._audit(event_type, project, request, payload)


def resolve_codex_bin() -> str:
    configured = os.environ.get("BEMINE_CODEX_BIN", "").strip()
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


def build_codex_prompt(project: ProjectChannel, request: DispatchRequest) -> str:
    attachment_lines = [
        f"- {path}" for path in request.attachments
    ] or ["- none"]
    return "\n".join(
        [
            "You are Bemine's Codex worker for a Discord-routed task.",
            f"Route reason: {request.route_reason or 'unspecified'}",
            f"Source channel: #{request.channel_name}",
            f"Project channel: #{project.channel_name}",
            f"Project id: {project.project_id or '(unknown)'}",
            f"Project name: {project.project_name or project.channel_name}",
            f"Project root: {project.cwd}",
            f"Discord message id: {request.message_id}",
            f"User: {request.author_name}",
            "",
            "User message:",
            request.content.strip() or "(no text; inspect attachments if useful)",
            "",
            "Saved attachments:",
            *attachment_lines,
            "",
            "Operational rules:",
            "- Work inside the project root unless the user clearly asks for coordination only.",
            "- Keep the final response concise and suitable for Discord.",
            "- If you changed files, mention the files and verification command.",
            "- If blocked or needing approval/credentials, say exactly what is needed.",
            "- Never print or expose secrets.",
        ]
    )


def _default_project_channels(*, repo_dir: Path, home: Path) -> dict[str, ProjectChannel]:
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
        return ProjectChannel(channel_name=name, cwd=Path(value).expanduser().resolve())
    if not isinstance(value, dict):
        raise ValueError(f"project channel {name!r} must be a string or object")
    cwd = Path(str(value["cwd"])).expanduser().resolve()
    runtime = str(value.get("runtime") or "codex")
    return ProjectChannel(
        channel_name=str(value.get("channel_name") or name),
        cwd=cwd,
        project_id=str(value.get("project_id") or ""),
        project_name=str(value.get("project_name") or value.get("name") or name),
        runtime=runtime,
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
    ordered = sorted(channels.values(), key=lambda channel: len(channel.channel_name), reverse=True)
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
        name_counts[_slug(str(row["name"]))] = name_counts.get(_slug(str(row["name"])), 0) + 1

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
    parts = [part for part in cwd.parts if part not in {"", "/", "home", "cdjk", "crew", "apps", ".worktrees"}]
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
        "runtime": channel.runtime,
    }
    if channel.project_id:
        payload["project_id"] = channel.project_id
    if channel.project_name:
        payload["project_name"] = channel.project_name
    return payload


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
    channels = await _discord_json(session, "GET", f"https://discord.com/api/v10/guilds/{guild_id}/channels")
    if not isinstance(channels, list):
        raise RuntimeError("Discord channel list response was not a list")
    categories = _sort_project_categories(
        [
            channel
            for channel in channels
            if channel.get("type") == 4 and _is_project_category_name(str(channel.get("name") or ""))
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


def _sort_project_categories(categories: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
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
) -> Mapping[str, Any]:
    return await _discord_json(
        session,
        "POST",
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        json_payload={"name": name, "type": 0, "parent_id": parent_id},
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
    async with session.request(method, url, json=dict(json_payload) if json_payload else None) as response:
        text = await response.text()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"raw": text}
        if response.status >= 400:
            raise RuntimeError(f"Discord API {method} {url} failed {response.status}: {data}")
        return data
