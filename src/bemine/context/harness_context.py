from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import config_int, config_list
from .operator_context import redact_operator_context


# Big enough to hold SOUL.md + SKILL.md + USER.md without truncating the agent's
# capability doc. Tunable via [agent].harness_context_limit. ~24k chars ≈ 6k tokens,
# trivial for any modern model's context window.
DEFAULT_HARNESS_CONTEXT_LIMIT = 24_000


@dataclass(frozen=True)
class HarnessContext:
    files: list[Path]
    text: str
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "files": [str(path) for path in self.files],
            "text": self.text,
            "truncated": self.truncated,
        }


def load_harness_context(
    *,
    repo_dir: Path | None = None,
    files: list[Path] | None = None,
    limit: int | None = None,
) -> HarnessContext:
    if limit is None:
        limit = config_int("agent", "harness_context_limit", DEFAULT_HARNESS_CONTEXT_LIMIT)
    if limit <= 0:
        raise ValueError("limit must be positive")
    paths = files if files is not None else _configured_harness_files(repo_dir=repo_dir)
    existing = [
        path.expanduser().resolve() for path in paths if path.expanduser().exists()
    ]
    blocks: list[str] = []
    for path in existing:
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks.append(f"# {path.name}\n\n{redact_operator_context(text).strip()}")
    combined = "\n\n---\n\n".join(blocks).strip()
    truncated = len(combined) > limit
    if truncated:
        combined = combined[:limit].rstrip() + "\n\n[harness context truncated]"
    return HarnessContext(files=existing, text=combined, truncated=truncated)


def render_harness_context(context: HarnessContext) -> str:
    if not context.files:
        return "No harness context files found.\n"
    lines = [
        "Harness context files:",
        *[f"- {path}" for path in context.files],
        "",
        context.text,
        "",
    ]
    return "\n".join(lines)


def _configured_harness_files(*, repo_dir: Path | None = None) -> list[Path]:
    raw = os.environ.get("BEMINE_HARNESS_CONTEXT_FILES", "").strip()
    if raw:
        return [
            Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()
        ]
    if repo_dir is None:
        configured = config_list("context", "harness_files")
        if configured:
            return [Path(part).expanduser() for part in configured]
    root = (repo_dir or Path(__file__).resolve().parents[2]).expanduser()
    return [root / "SOUL.md", root / "SKILL.md"]
