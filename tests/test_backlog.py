from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bemine.backlog import add_backlog_item, list_backlog_items
from bemine.cli import main
from bemine.config import BemineSettings
from bemine.db import BemineDB


def test_add_backlog_item_writes_vault_and_audit(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)

    entry = add_backlog_item(
        settings,
        owner="SMM",
        text="post the Rai checkpoint chart",
        source="test",
        project="rai-speech-toolkit",
        db=db,
        when=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )

    assert entry.path == tmp_path / "vault" / "Backlog" / "SMM.md"
    text = entry.path.read_text(encoding="utf-8")
    assert "# SMM Backlog" in text
    assert "post the Rai checkpoint chart" in text
    assert "source=test" in text
    assert "project=rai-speech-toolkit" in text
    rows = db.list_audit_events(category="backlog", limit=1)
    assert rows[0]["event_type"] == "backlog_item_added"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["owner"] == "SMM"


def test_backlog_rejects_unknown_owner(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "bemine.toml"
    config.write_text(
        """
[backlog]
owners = ["PA", "SMM"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config))
    settings = BemineSettings.load(state_dir=tmp_path / "state", vault_dir=tmp_path / "vault")

    with pytest.raises(ValueError, match="unknown backlog owner"):
        add_backlog_item(settings, owner="ops", text="x")


def test_cli_backlog_add_and_list(tmp_path: Path, capsys) -> None:
    main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "backlog",
            "add",
            "follow up on stuck asks",
            "--owner",
            "PA",
            "--project",
            "bm",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["owner"] == "PA"
    assert Path(payload["path"]).exists()

    main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "backlog",
            "list",
            "--owner",
            "PA",
            "--json",
        ]
    )
    listed = json.loads(capsys.readouterr().out)
    assert listed["owner"] == "PA"
    assert "follow up on stuck asks" in listed["items"][0]


def test_list_backlog_items_returns_recent_items(tmp_path: Path) -> None:
    settings = BemineSettings.load(state_dir=tmp_path / "state", vault_dir=tmp_path / "vault")
    add_backlog_item(settings, owner="SMM", text="first")
    add_backlog_item(settings, owner="SMM", text="second")

    assert len(list_backlog_items(settings, owner="SMM", limit=1)) == 1
    assert "second" in list_backlog_items(settings, owner="SMM", limit=1)[0]
