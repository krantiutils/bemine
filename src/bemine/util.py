from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def stable_id(value: str, *, prefix: str = "") -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest}" if prefix else digest


def project_name_from_cwd(cwd: str) -> str:
    if not cwd:
        return "unknown"
    path = Path(cwd)
    name = path.name or path.parent.name or "unknown"
    return name


def project_id_from_cwd(cwd: str) -> str:
    return stable_id(str(Path(cwd)), prefix="proj_")


def safe_filename(value: str, fallback: str = "item") -> str:
    cleaned = SLUG_RE.sub("_", value).strip("._ ")
    return cleaned or fallback


def coerce_timestamp(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def compact_text(value: str, *, limit: int = 280) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."

