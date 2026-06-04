from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default.expanduser().resolve()


@dataclass(frozen=True)
class BemineSettings:
    state_dir: Path
    db_path: Path
    vault_dir: Path
    codex_home: Path
    claude_home: Path
    kimi_home: Path
    device_id: str

    @classmethod
    def load(
        cls,
        *,
        state_dir: Path | None = None,
        vault_dir: Path | None = None,
        codex_home: Path | None = None,
        claude_home: Path | None = None,
        kimi_home: Path | None = None,
        device_id: str | None = None,
    ) -> "BemineSettings":
        cwd = Path.cwd()
        resolved_state = (
            state_dir.expanduser().resolve()
            if state_dir
            else _env_path("BEMINE_HOME", cwd / ".bemine")
        )
        resolved_vault = (
            vault_dir.expanduser().resolve()
            if vault_dir
            else _env_path("BEMINE_VAULT", Path.home() / "Documents" / "Mayor")
        )
        resolved_codex = (
            codex_home.expanduser().resolve()
            if codex_home
            else _env_path("BEMINE_CODEX_HOME", Path.home() / ".codex")
        )
        resolved_claude = (
            claude_home.expanduser().resolve()
            if claude_home
            else _env_path("BEMINE_CLAUDE_HOME", Path.home() / ".claude")
        )
        resolved_kimi = (
            kimi_home.expanduser().resolve()
            if kimi_home
            else _env_path("BEMINE_KIMI_HOME", Path.home() / "devlog" / "raw" / "kimi")
        )
        resolved_device = (
            device_id
            or os.environ.get("BEMINE_DEVICE_ID", "").strip()
            or os.uname().nodename
        )
        return cls(
            state_dir=resolved_state,
            db_path=resolved_state / "bemine.db",
            vault_dir=resolved_vault,
            codex_home=resolved_codex,
            claude_home=resolved_claude,
            kimi_home=resolved_kimi,
            device_id=resolved_device,
        )

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)

