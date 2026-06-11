from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .backlog import list_backlog_items
from .config import BemineSettings, config_int, config_list, config_string
from .db import BemineDB
from .index.status import _redact
from .index.vault import ObsidianVault
from .util import compact_text, safe_filename


DEFAULT_SMM_LIMIT = 3
DEFAULT_TARGET_CHANNELS = ["X", "LinkedIn"]
EXPECTED_ASSETS = ("logo.png", "wordmark.svg", "wordmark-dev.svg")
EXPECTED_ASSET_SOURCES = {
    "logo.png": "https://ampixa.com/assets/logo.png",
    "wordmark.svg": "https://ampixa.com/assets/wordmark.svg",
    "wordmark-dev.svg": "https://ampixa.com/assets/wordmark-dev.svg",
}
PROJECT_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_.-]{2,})\b")
DEFAULT_REJECT_TERMS = [
    "already completed",
    "authorize termination",
    "aws sso",
    "blocked",
    "destructive",
    "dispatch failed",
    "fallback",
    "failed",
    "irreversible",
    "let me summarize",
    "not done",
    "now i have a complete picture",
    "password",
    "private",
    "previous turn",
    "should i",
    "sshpass",
    "stale",
    "token",
    "want me",
]


@dataclass(frozen=True)
class SMMAsset:
    name: str
    path: Path
    exists: bool
    size_bytes: int = 0
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class SMMDraft:
    source: str
    signal: str
    project: str
    hook: str
    body: str
    proof: str
    asset_prompt: str
    target_channels: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "signal": self.signal,
            "project": self.project,
            "hook": self.hook,
            "body": self.body,
            "proof": self.proof,
            "asset_prompt": self.asset_prompt,
            "target_channels": self.target_channels,
        }


@dataclass(frozen=True)
class SMMDraftResult:
    generated_at: str
    drafts: list[SMMDraft]
    assets: list[SMMAsset]
    style_path: Path
    style_exists: bool
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "drafts": [draft.to_dict() for draft in self.drafts],
            "assets": [asset.to_dict() for asset in self.assets],
            "style_path": str(self.style_path),
            "style_exists": self.style_exists,
            "path": str(self.path) if self.path else "",
        }


def draft_smm_posts(
    settings: BemineSettings,
    *,
    topic: str = "",
    project: str = "",
    limit: int = DEFAULT_SMM_LIMIT,
    write: bool = False,
    db: BemineDB | None = None,
    project_root: Path | None = None,
    generated_at: datetime | None = None,
) -> SMMDraftResult:
    db = db or BemineDB(settings.db_path)
    project_root = (project_root or Path.cwd()).expanduser().resolve()
    generated_at = generated_at or datetime.now(timezone.utc)
    limit = _draft_limit(limit)
    target_channels = smm_target_channels()
    style_path = smm_style_path(project_root)
    assets = collect_ampixa_assets(project_root)
    signals = _collect_smm_signals(settings, topic=topic, limit=max(limit * 6, 12))
    drafts: list[SMMDraft] = []
    seen: set[str] = set()
    for source, signal in signals:
        clean_signal = _clean_public_signal(signal)
        if not _public_signal_usable(clean_signal):
            continue
        normalized = _normalize_signal(clean_signal)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        drafts.append(
            _draft_from_signal(
                source,
                clean_signal,
                project=project,
                target_channels=target_channels,
            )
        )
        if len(drafts) >= limit:
            break

    path = None
    if write:
        path = smm_draft_path(settings, generated_at=generated_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        result = SMMDraftResult(
            generated_at=generated_at.isoformat(),
            drafts=drafts,
            assets=assets,
            style_path=style_path,
            style_exists=style_path.exists(),
            path=path,
        )
        path.write_text(render_smm_drafts_markdown(result), encoding="utf-8")
        db.record_audit_event(
            category="smm",
            event_type="smm_drafts_recorded",
            actor="smm",
            device_id=settings.device_id,
            project_id=project or None,
            payload_json=json.dumps(
                {
                    "path": str(path),
                    "drafts": len(drafts),
                    "topic": compact_text(topic, limit=300),
                    "project": project,
                },
                sort_keys=True,
            ),
        )

    return SMMDraftResult(
        generated_at=generated_at.isoformat(),
        drafts=drafts,
        assets=assets,
        style_path=style_path,
        style_exists=style_path.exists(),
        path=path,
    )


def collect_ampixa_assets(project_root: Path | None = None) -> list[SMMAsset]:
    root = (project_root or Path.cwd()).expanduser().resolve()
    asset_dir = smm_asset_dir(root)
    assets: list[SMMAsset] = []
    for name in EXPECTED_ASSETS:
        path = asset_dir / name
        exists = path.exists()
        assets.append(
            SMMAsset(
                name=name,
                path=path,
                exists=exists,
                size_bytes=path.stat().st_size if exists else 0,
                source_url=EXPECTED_ASSET_SOURCES.get(name, ""),
            )
        )
    return assets


def render_smm_drafts(result: SMMDraftResult) -> str:
    lines = [
        "SMM drafts:",
        f"- drafts: {len(result.drafts)}",
        f"- style: `{result.style_path}` ({'present' if result.style_exists else 'missing'})",
    ]
    if result.path:
        lines.append(f"- path: `{result.path}`")
    missing = [asset.name for asset in result.assets if not asset.exists]
    if missing:
        lines.append(f"- missing assets: {', '.join(missing)}")
    for index, draft in enumerate(result.drafts, start=1):
        lines.extend(
            [
                "",
                f"## Draft {index}: {draft.project or 'Ampixa'}",
                draft.hook,
                "",
                draft.body,
                "",
                f"Proof: {draft.proof}",
                f"Asset prompt: {draft.asset_prompt}",
                f"Targets: {', '.join(draft.target_channels)}",
            ]
        )
    if not result.drafts:
        lines.extend(
            [
                "",
                "No SMM draft signals found. Run `bemine brief run` or add an SMM backlog item first.",
            ]
        )
    return "\n".join(lines) + "\n"


def render_smm_drafts_markdown(result: SMMDraftResult) -> str:
    lines = [
        "# SMM Drafts",
        "",
        f"Generated: `{result.generated_at}`",
        f"Style: `{result.style_path}`",
        "",
        "## Assets",
        "",
    ]
    for asset in result.assets:
        status = "present" if asset.exists else "missing"
        lines.append(f"- `{asset.name}`: {status} `{asset.path}`")
    lines.extend(["", "## Drafts", ""])
    if not result.drafts:
        lines.append("No draft signals found.")
    for index, draft in enumerate(result.drafts, start=1):
        lines.extend(
            [
                f"### Draft {index}: {draft.project or 'Ampixa'}",
                "",
                f"Source: `{draft.source}`",
                "",
                "Hook:",
                draft.hook,
                "",
                "Body:",
                draft.body,
                "",
                f"Proof: {draft.proof}",
                "",
                f"Asset prompt: {draft.asset_prompt}",
                "",
                f"Targets: {', '.join(draft.target_channels)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def smm_draft_status(
    settings: BemineSettings,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    output_dir = smm_draft_dir(settings)
    paths = (
        sorted(
            output_dir.glob("*-smm-drafts.md"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if output_dir.exists()
        else []
    )
    assets = collect_ampixa_assets(project_root)
    style_path = smm_style_path(project_root)
    status: dict[str, Any] = {
        "schema": "bemine.smm_draft_status.v1",
        "output_dir": str(output_dir),
        "exists": bool(paths),
        "count": len(paths),
        "latest": {},
        "style_path": str(style_path),
        "style_exists": style_path.exists(),
        "assets": [asset.to_dict() for asset in assets],
        "missing_assets": [asset.name for asset in assets if not asset.exists],
        "target_channels": smm_target_channels(),
    }
    if not paths:
        return status
    latest = paths[0]
    text = latest.read_text(encoding="utf-8", errors="replace")
    status["latest"] = {
        "path": str(latest),
        "generated_at": _draft_generated_at(text),
        "mtime": datetime.fromtimestamp(
            latest.stat().st_mtime, timezone.utc
        ).isoformat(),
        "drafts": _draft_count(text),
    }
    return status


def render_smm_draft_status(status: Mapping[str, Any]) -> str:
    lines = [
        "SMM draft status:",
        f"- output_dir: `{status['output_dir']}`",
        f"- draft_files: {status.get('count', 0)}",
        f"- style: `{status.get('style_path')}` ({'present' if status.get('style_exists') else 'missing'})",
        f"- target_channels: {', '.join(status.get('target_channels') or []) or 'none'}",
    ]
    missing_assets = status.get("missing_assets") or []
    lines.append(
        f"- missing_assets: {', '.join(missing_assets) if missing_assets else 'none'}"
    )
    assets = status.get("assets") or []
    if assets:
        lines.append("- assets:")
        for asset in assets:
            present = "present" if asset.get("exists") else "missing"
            size = int(asset.get("size_bytes") or 0)
            lines.append(
                f"  - {asset.get('name')}: {present} {size}B `{asset.get('path')}`"
            )
    latest = status.get("latest") or {}
    if not latest:
        lines.append("- latest: none")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            f"- latest_path: `{latest.get('path')}`",
            f"- latest_generated_at: {latest.get('generated_at') or 'unknown'}",
            f"- file_mtime: {latest.get('mtime') or 'unknown'}",
            f"- latest_drafts: {latest.get('drafts', 0)}",
        ]
    )
    return "\n".join(lines) + "\n"


def smm_draft_path(
    settings: BemineSettings,
    *,
    generated_at: datetime | None = None,
) -> Path:
    generated_at = generated_at or datetime.now(timezone.utc)
    ObsidianVault(settings.vault_dir)
    stem = safe_filename(f"{generated_at.date().isoformat()}-smm-drafts", "smm-drafts")
    return smm_draft_dir(settings) / f"{stem}.md"


def smm_draft_dir(settings: BemineSettings) -> Path:
    configured = config_string("smm", "output_dir", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else settings.vault_dir / "SMM" / "Drafts"
    )


def smm_target_channels() -> list[str]:
    return config_list("smm", "target_channels", DEFAULT_TARGET_CHANNELS) or list(
        DEFAULT_TARGET_CHANNELS
    )


def smm_asset_dir(project_root: Path | None = None) -> Path:
    root = (project_root or Path.cwd()).expanduser().resolve()
    configured = config_string("smm", "asset_dir", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else root / "assets" / "ampixa"
    )


def smm_style_path(project_root: Path | None = None) -> Path:
    root = (project_root or Path.cwd()).expanduser().resolve()
    configured = config_string("smm", "style_path", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else root / "subagents" / "style.md"
    )


def _draft_generated_at(text: str) -> str:
    match = re.search(r"^Generated:\s*`([^`]+)`", text, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _draft_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("### Draft "))


def _draft_limit(limit: int) -> int:
    configured = config_int("smm", "draft_limit", DEFAULT_SMM_LIMIT)
    requested = limit if limit and limit > 0 else configured
    return max(1, min(10, requested))


def _collect_smm_signals(
    settings: BemineSettings,
    *,
    topic: str,
    limit: int,
) -> list[tuple[str, str]]:
    signals: list[tuple[str, str]] = []
    if topic.strip():
        return [("topic", _redact(topic.strip()))]
    for line in reversed(list_backlog_items(settings, owner="SMM", limit=limit)):
        signals.append(("backlog:SMM", _strip_task_line(line)))
    for line in _latest_brief_smm_lines(settings, limit=limit):
        signals.append(("brief:SMM", _strip_task_line(line)))
    return [(source, signal) for source, signal in signals if signal]


def _latest_brief_smm_lines(settings: BemineSettings, *, limit: int) -> list[str]:
    brief_dir = settings.vault_dir / "Briefs"
    if not brief_dir.exists():
        return []
    paths = sorted(
        brief_dir.glob("*-pa-smm.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not paths:
        return []
    in_smm = False
    lines: list[str] = []
    for raw in paths[0].read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if stripped == "## SMM":
            in_smm = True
            continue
        if in_smm and stripped.startswith("## "):
            break
        if in_smm and stripped.startswith("- [ ] "):
            lines.append(stripped)
            if len(lines) >= limit:
                break
    return lines


def _strip_task_line(line: str) -> str:
    text = _redact(line).strip()
    text = re.sub(r"^-\s+\[\s*\]\s+", "", text)
    text = re.sub(r"^\d{4}-\d{2}-\d{2}T[^—]+—\s*", "", text)
    text = re.sub(r"\s*<!--.*?-->\s*$", "", text)
    text = re.sub(r"\s+_\(project:.*?\)_\s*$", "", text)
    return " ".join(text.split())


def _draft_from_signal(
    source: str,
    signal: str,
    *,
    project: str,
    target_channels: list[str],
) -> SMMDraft:
    clean = compact_text(_strip_task_line(signal), limit=260)
    inferred_project = project.strip() or _project_from_signal(clean)
    subject = inferred_project or "Ampixa"
    hook = _hook(subject, clean)
    proof = _proof(clean)
    body = "\n".join(
        [
            "Why it matters: Nepali and regional speech work needs visible, testable artifacts, not vague model claims.",
            f"Concrete detail: {proof}",
            "",
            "The useful story is the pipeline: data, evaluation, alignment, and real audio conditions getting a little less fragile.",
        ]
    )
    return SMMDraft(
        source=source,
        signal=clean,
        project=subject,
        hook=hook,
        body=body,
        proof=proof,
        asset_prompt=_asset_prompt(subject, clean),
        target_channels=target_channels,
    )


def _hook(project: str, signal: str) -> str:
    if any(
        word in signal.casefold()
        for word in ("shipped", "deployed", "released", "built")
    ):
        return f"{project}: shipped a concrete speech-infra update."
    if any(
        word in signal.casefold()
        for word in ("benchmark", "eval", "checkpoint", "chr", "bleu")
    ):
        return f"{project}: a measured speech-infra result is ready to explain."
    return f"{project}: a useful speech-infra progress note is worth drafting."


def _proof(signal: str) -> str:
    cleaned = compact_text(signal, limit=180)
    return cleaned.rstrip(".") + "."


def _asset_prompt(project: str, signal: str) -> str:
    return (
        "Create a text-first Ampixa visual on a white background with black monospace-like "
        "type, crimson #DC143C accent lines, and the local Ampixa logo/wordmark. "
        f"Title: {project}. Show one concrete artifact from this signal: "
        f"{compact_text(signal, limit=140)}. Avoid hype, gradients, and stock imagery."
    )


def _project_from_signal(signal: str) -> str:
    for candidate in PROJECT_RE.findall(signal):
        normalized = candidate.strip("`.,:;()[]")
        lowered = normalized.casefold()
        if lowered in {
            "draft",
            "turn",
            "this",
            "progress",
            "social",
            "post",
            "idea",
            "project",
            "update",
            "nepali",
            "regional",
            "speech",
            "infrastructure",
        }:
            continue
        if "-" in normalized or lowered in {"rai", "phantom", "bemine", "ampixa"}:
            return normalized
    return ""


def _clean_public_signal(signal: str) -> str:
    text = _strip_task_line(signal)
    for prefix in (
        "Turn this progress into a social post idea:",
        "Draft a project update from",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    text = re.sub(r"^`([^`]+)`:\s*", r"\1: ", text)
    text = re.sub(r"\s+\|\s+#\s+\|.*$", "", text)
    text = re.sub(r"\s+##\s+", " ", text)
    return compact_text(" ".join(text.split()), limit=260)


def _public_signal_usable(signal: str) -> bool:
    if not signal:
        return False
    haystack = signal.casefold()
    reject_terms = config_list("smm", "reject_terms", DEFAULT_REJECT_TERMS)
    return not any(term.casefold() in haystack for term in reject_terms)


def _normalize_signal(signal: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", signal.casefold()).strip()
