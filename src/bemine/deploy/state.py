from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class DeployState:
    phase: str = "idle"  # idle|requested|deploying|daemon_refresh|confirmed|rolled_back|failed
    target_sha: str = ""
    rollback_sha: str = ""
    channel_id: str = ""
    requested_by: str = ""
    boot_attempts: int = 0
    boot_ts: float = 0.0
    requested_at: float = 0.0
    updated_at: float = 0.0

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), sort_keys=True, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "DeployState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


def state_path(settings) -> Path:
    return settings.state_dir / "deploy" / "state.json"
