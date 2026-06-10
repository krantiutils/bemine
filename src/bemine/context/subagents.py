from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import config_int, config_list, config_path
from .operator_context import redact_operator_context


DEFAULT_SUBAGENT_CONTEXT_LIMIT = 12_000


@dataclass(frozen=True)
class SubagentProfile:
    name: str
    path: Path
    text: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "path": str(self.path),
            "text": self.text,
        }


@dataclass(frozen=True)
class SubagentContext:
    profiles: list[SubagentProfile]
    text: str
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "profiles": [profile.to_dict() for profile in self.profiles],
            "text": self.text,
            "truncated": self.truncated,
        }


def load_subagent_context(
    *,
    files: list[Path] | None = None,
    limit: int | None = None,
) -> SubagentContext:
    if limit is None:
        limit = config_int(
            "subagents", "context_limit", DEFAULT_SUBAGENT_CONTEXT_LIMIT
        )
    if limit <= 0:
        raise ValueError("limit must be positive")
    paths = files if files is not None else _configured_subagent_files()
    profiles: list[SubagentProfile] = []
    blocks: list[str] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        text = redact_operator_context(
            resolved.read_text(encoding="utf-8", errors="replace")
        ).strip()
        profile = SubagentProfile(
            name=resolved.stem,
            path=resolved,
            text=text,
        )
        profiles.append(profile)
        blocks.append(text if text.startswith("#") else f"# {profile.name}\n\n{text}")
    combined = "\n\n---\n\n".join(blocks).strip()
    truncated = len(combined) > limit
    if truncated:
        combined = combined[:limit].rstrip() + "\n\n[subagent context truncated]"
    return SubagentContext(profiles=profiles, text=combined, truncated=truncated)


def render_subagent_context(context: SubagentContext) -> str:
    if not context.profiles:
        return "No subagent profiles configured.\n"
    lines = [
        "Subagent profiles:",
        *[f"- {profile.name}: `{profile.path}`" for profile in context.profiles],
        "",
        context.text,
        "",
    ]
    return "\n".join(lines)


def _configured_subagent_files() -> list[Path]:
    raw = os.environ.get("BEMINE_SUBAGENT_FILES", "").strip()
    if raw:
        return [
            Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()
        ]
    configured = config_list("subagents", "files")
    if configured:
        base = config_path().parent
        return [
            Path(part).expanduser()
            if Path(part).expanduser().is_absolute()
            else base / part
            for part in configured
        ]
    default_dir = config_path().parent / "subagents"
    if default_dir.exists():
        return sorted(default_dir.glob("*.md"))
    return []
