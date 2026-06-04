from __future__ import annotations

import os
from pathlib import Path


def ignored_project_reason(cwd: str | Path, *, name: str = "") -> str | None:
    path = Path(str(cwd)).expanduser().resolve()
    for root in ignored_project_roots():
        if path == root or root in path.parents:
            return f"ignored project root: {root}"
    return None


def ignored_project_roots() -> list[Path]:
    raw = os.environ.get("BEMINE_IGNORED_PROJECT_ROOTS", "").strip()
    if raw:
        roots = [part.strip() for part in raw.split(os.pathsep) if part.strip()]
    else:
        roots = [str(Path.home() / "gt")]
    return [Path(root).expanduser().resolve() for root in roots]
