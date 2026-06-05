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

from bemine.config import BemineSettings, config_path, config_string
from bemine.db import BemineDB
from bemine.harness_context import load_harness_context
from bemine.kimi_handler import (
    KimiHandler,
    load_kimi_config,
    resolve_kimi_api_key,
)
from bemine.operator_context import load_operator_context
from bemine.project_filters import ignored_project_reason
from bemine.status import _redact

PROJECT_CATEGORY_NAME = "Bemine Projects"
DISPATCH_CONFIG_NAME = "discord-projects.json"
MAX_CHANNELS_PER_CATEGORY = 50

Runner = Callable[..., subprocess.CompletedProcess[str]]
ProgressCallback = Callable[["DispatchProgress"], None]


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


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    project: ProjectChannel
    output: str
    elapsed_seconds: float
    returncode: int | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class DispatchProgress:
    runtime: str
    event_type: str
    summary: str
    raw_event: Mapping[str, Any]


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
        channel.channel_name: {"cwd": str(channel.cwd)}
        for channel in defaults.values()
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
        project_category_ids = {
            str(category.get("id") or "") for category in categories
        }
        existing_project_channels = [
            channel
            for channel in existing
            if channel.get("type") == 0
            and str(channel.get("parent_id") or "") in project_category_ids
        ]
        existing_by_name = {
            str(channel.get("name")): channel for channel in existing_project_channels
        }
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
            channel = await _create_text_channel(
                session, guild_id, name, parent_id=str(category["id"])
            )
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
                "category_ids": [
                    str(category.get("id") or "") for category in categories
                ],
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
        "category_names": [
            str(category.get("name") or PROJECT_CATEGORY_NAME)
            for category in categories
        ],
        "created": created,
        "deleted": deleted,
        "deleted_categories": deleted_categories,
        "skipped": skipped,
        "config_path": str(config_path),
        "project_count": len(channels),
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
            dispatcher = self.kimi_dispatcher or KimiDispatcher(self.settings)
            return dispatcher.dispatch(project, request, progress_callback)
        if runtime == "codex":
            dispatcher = self.codex_dispatcher or CodexDispatcher(self.settings)
            return dispatcher.dispatch(project, request, progress_callback)
        return DispatchResult(
            ok=False,
            project=project,
            output=(
                f"Unsupported dispatch runtime `{runtime}` for `#{project.channel_name}`. "
                "Use `kimi` or `codex`."
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
        timeout_seconds: int = 900,
        kimi_bin: str | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or subprocess.run
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
        del progress_callback
        started = time.monotonic()
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
            "elapsed_seconds": round(result.elapsed_seconds, 3),
        }
        if extra:
            payload.update(extra)
        self._audit(event_type, project, request, payload)


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
        return "danger-full-access"
    if configured not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(
            "BEMINE_CODEX_SANDBOX must be one of: read-only, workspace-write, danger-full-access"
        )
    return configured


def resolve_codex_model() -> str:
    return os.environ.get("BEMINE_CODEX_MODEL", "").strip() or config_string(
        "codex", "model", ""
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
                "kimi_bin": resolve_kimi_bin(),
                "tool_calls": "available through kimi --print --yolo CLI tools",
            }
        )
    elif runtime == "codex":
        model = resolve_codex_model()
        payload.update(
            {
                "provider": "codex",
                "model": model or "codex config/default",
                "model_source": "BEMINE_CODEX_MODEL"
                if os.environ.get("BEMINE_CODEX_MODEL", "").strip()
                else "bemine.toml"
                if model
                else "$CODEX_HOME/config.toml",
                "codex_bin": resolve_codex_bin(),
                "sandbox": resolve_codex_sandbox(),
                "tool_calls": "available through codex exec --json progress events",
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
