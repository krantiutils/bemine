from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--device",
            "testbox",
            *args,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_audit_list_json_parses_payload_and_filters(tmp_path: Path) -> None:
    db = _seed_audit_events(tmp_path)

    rows = json.loads(
        _run(
            tmp_path,
            "audit",
            "list",
            "--category",
            "worker",
            "--event-type",
            "worker_transfer",
            "--json",
        ).stdout
    )

    assert len(rows) == 1
    assert rows[0]["category"] == "worker"
    assert rows[0]["event_type"] == "worker_transfer"
    assert rows[0]["payload"]["target"]["transport"] == "ssh-tmux"
    assert "payload_json" not in rows[0]
    assert db.counts()["audit_events"] == 2


def test_audit_list_human_output_summarizes_transfer(tmp_path: Path) -> None:
    _seed_audit_events(tmp_path)

    text = _run(
        tmp_path,
        "audit",
        "list",
        "--project-id",
        "bm",
        "--limit",
        "5",
    ).stdout

    assert "Audit events:" in text
    assert "worker/worker_transfer" in text
    assert "source[codex] -> target[claude/ssh-tmux] duplicates=1" in text
    assert "runtime/kimi_ask" not in text


def _seed_audit_events(tmp_path: Path) -> BemineDB:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    db.record_audit_event(
        category="runtime",
        event_type="kimi_ask",
        actor="test",
        device_id="testbox",
        project_id="other",
        payload_json=json.dumps({"provider": "moonshot"}, sort_keys=True),
    )
    db.record_audit_event(
        category="worker",
        event_type="worker_transfer",
        actor="worker-controller",
        device_id="testbox",
        project_id="bm",
        payload_json=json.dumps(
            {
                "source": {
                    "name": "source",
                    "runtime": "codex",
                    "transport": "tmux",
                },
                "target": {
                    "name": "target",
                    "runtime": "claude",
                    "transport": "ssh-tmux",
                },
                "duplicate_heads": [{"name": "other-head"}],
            },
            sort_keys=True,
        ),
    )
    return db
