from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import config_list


DEFAULT_CONTEXT_LIMIT = 12_000
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_-]*(?:"
    r"api[_-]?key|secret(?:[_-]?api[_-]?key)?|password|passwd|token|"
    r"access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|private[_-]?key"
    r")[A-Z0-9_-]*)(\s*[:=]\s*)([^\s'\"`]+)"
)
TOKEN_RE = re.compile(
    r"\b(?:"
    r"AKIA[0-9A-Z]{16}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"hf_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}"
    r")\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


@dataclass(frozen=True)
class OperatorContext:
    files: list[Path]
    text: str
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "files": [str(path) for path in self.files],
            "text": self.text,
            "truncated": self.truncated,
        }


def load_operator_context(
    *,
    home: Path | None = None,
    files: list[Path] | None = None,
    limit: int = DEFAULT_CONTEXT_LIMIT,
) -> OperatorContext:
    if limit <= 0:
        raise ValueError("limit must be positive")
    paths = files if files is not None else _configured_context_files(home=home)
    existing = [
        path.expanduser().resolve() for path in paths if path.expanduser().exists()
    ]
    blocks: list[str] = []
    for path in existing:
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks.append(f"# {path}\n\n{redact_operator_context(text).strip()}")
    combined = "\n\n---\n\n".join(blocks).strip()
    truncated = len(combined) > limit
    if truncated:
        combined = combined[:limit].rstrip() + "\n\n[operator context truncated]"
    return OperatorContext(files=existing, text=combined, truncated=truncated)


def render_operator_context(context: OperatorContext) -> str:
    if not context.files:
        return "No operator context files found.\n"
    lines = [
        "Operator context files:",
        *[f"- {path}" for path in context.files],
        "",
        context.text,
        "",
    ]
    return "\n".join(lines)


def redact_operator_context(text: str) -> str:
    text = PRIVATE_KEY_RE.sub("<private-key-redacted>", text)
    text = TOKEN_RE.sub("<token-redacted>", text)
    text = SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text
    )
    return text


def _configured_context_files(*, home: Path | None = None) -> list[Path]:
    raw = os.environ.get("BEMINE_OPERATOR_CONTEXT_FILES", "").strip()
    if raw:
        return [
            Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()
        ]
    configured = config_list("context", "operator_files")
    if configured:
        return [Path(part).expanduser() for part in configured]
    base_home = (home or Path.home()).expanduser()
    return [base_home / "deploy.md"]
