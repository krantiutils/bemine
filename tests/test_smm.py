from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bemine.backlog import add_backlog_item
from bemine.cli import main
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.smm import collect_ampixa_assets, draft_smm_posts


def _settings(tmp_path: Path) -> tuple[BemineSettings, BemineDB]:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        device_id="testbox",
    )
    return settings, BemineDB(settings.db_path)


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "bm"
    (root / "subagents").mkdir(parents=True)
    (root / "assets" / "ampixa").mkdir(parents=True)
    (root / "subagents" / "style.md").write_text(
        "# Ampixa Style\nUse crimson #DC143C and concrete proof.\n",
        encoding="utf-8",
    )
    for name in ("logo.png", "wordmark.svg", "wordmark-dev.svg"):
        (root / "assets" / "ampixa" / name).write_bytes(b"asset")
    return root


def _empty_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "bemine.toml"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("BEMINE_CONFIG", str(config_path))


def test_collect_ampixa_assets_reports_expected_files(tmp_path: Path, monkeypatch) -> None:
    _empty_config(tmp_path, monkeypatch)
    root = _project_root(tmp_path)

    assets = collect_ampixa_assets(root)

    assert {asset.name for asset in assets} == {
        "logo.png",
        "wordmark.svg",
        "wordmark-dev.svg",
    }
    assert all(asset.exists for asset in assets)
    assert all(asset.size_bytes == 5 for asset in assets)


def test_draft_smm_posts_uses_topic_backlog_brief_assets_and_writes_vault(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _empty_config(tmp_path, monkeypatch)
    settings, db = _settings(tmp_path)
    root = _project_root(tmp_path)
    add_backlog_item(
        settings,
        owner="SMM",
        text="Rai checkpoint benchmark passed chrF 24.78 with cleaner eval chart",
        project="rai-speech-toolkit",
        db=db,
    )
    brief = settings.vault_dir / "Briefs" / "2026-06-11-pa-smm.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(
        "\n".join(
            [
                "# PA/SMM Brief",
                "",
                "## PA",
                "- [ ] review blockers",
                "",
                "## SMM",
                "- [ ] Turn this progress into a social post idea: streaming-tts released a demo.",
            ]
        ),
        encoding="utf-8",
    )
    generated_at = datetime(2026, 6, 11, tzinfo=timezone.utc)

    result = draft_smm_posts(
        settings,
        project="rai-speech-toolkit",
        limit=3,
        write=True,
        db=db,
        project_root=root,
        generated_at=generated_at,
    )

    assert len(result.drafts) == 2
    assert result.drafts[0].project == "rai-speech-toolkit"
    assert "Nepali and regional speech" in result.drafts[0].body
    assert "#DC143C" in result.drafts[0].asset_prompt
    assert result.path == settings.vault_dir / "SMM" / "Drafts" / "2026-06-11-smm-drafts.md"
    assert result.path.exists()
    assert "Rai checkpoint benchmark" in result.path.read_text(encoding="utf-8")
    rows = db.list_audit_events(category="smm", limit=1)
    assert rows[0]["event_type"] == "smm_drafts_recorded"


def test_smm_draft_explicit_topic_does_not_fallback_to_backlog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _empty_config(tmp_path, monkeypatch)
    settings, db = _settings(tmp_path)
    root = _project_root(tmp_path)
    add_backlog_item(
        settings,
        owner="SMM",
        text="Rai checkpoint benchmark passed chrF 24.78 with cleaner eval chart",
        project="rai-speech-toolkit",
        db=db,
    )

    result = draft_smm_posts(
        settings,
        topic="Rai alignment demo shipped with before/after audio",
        project="rai-speech-toolkit",
        limit=3,
        db=db,
        project_root=root,
    )
    rejected = draft_smm_posts(
        settings,
        topic="That fallback is stale because the previous turn already completed it",
        limit=3,
        db=db,
        project_root=root,
    )

    assert len(result.drafts) == 1
    assert result.drafts[0].source == "topic"
    assert "Rai alignment demo shipped" in result.drafts[0].signal
    assert rejected.drafts == []


def test_cli_smm_draft_json(tmp_path: Path, capsys, monkeypatch) -> None:
    _empty_config(tmp_path, monkeypatch)
    settings, db = _settings(tmp_path)
    root = _project_root(tmp_path)
    add_backlog_item(
        settings,
        owner="SMM",
        text="neptts-bench released a benchmark chart",
        db=db,
    )
    monkeypatch.chdir(root)

    main([
        "--state-dir",
        str(settings.state_dir),
        "--vault",
        str(settings.vault_dir),
        "smm",
        "draft",
        "--limit",
        "1",
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["drafts"]) == 1
    assert payload["assets"][0]["exists"] is True
    assert "neptts-bench" in payload["drafts"][0]["signal"]


def test_smm_draft_respects_config_and_rejects_stale_operational_signals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, db = _settings(tmp_path)
    root = _project_root(tmp_path)
    output_dir = tmp_path / "draft-output"
    config_path = tmp_path / "bemine.toml"
    config_path.write_text(
        f"""
[smm]
draft_limit = 2
target_channels = ["X", "LinkedIn", "Mastodon"]
style_path = "{root / "subagents" / "style.md"}"
asset_dir = "{root / "assets" / "ampixa"}"
output_dir = "{output_dir}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEMINE_CONFIG", str(config_path))
    add_backlog_item(
        settings,
        owner="SMM",
        text="That fallback is stale because the previous turn already completed it",
        db=db,
    )
    add_backlog_item(
        settings,
        owner="SMM",
        text="Rai checkpoint benchmark passed chrF 24.78 with a cleaner eval chart",
        project="rai-speech-toolkit",
        db=db,
    )
    generated_at = datetime(2026, 6, 11, tzinfo=timezone.utc)

    result = draft_smm_posts(
        settings,
        limit=0,
        write=True,
        db=db,
        project_root=root,
        generated_at=generated_at,
    )

    assert len(result.drafts) == 1
    assert "fallback" not in result.drafts[0].signal.casefold()
    assert result.drafts[0].target_channels == ["X", "LinkedIn", "Mastodon"]
    assert result.style_path == root / "subagents" / "style.md"
    assert result.assets[0].path == root / "assets" / "ampixa" / "logo.png"
    assert result.path == output_dir / "2026-06-11-smm-drafts.md"
