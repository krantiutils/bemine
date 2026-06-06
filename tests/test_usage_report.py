from pathlib import Path
from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.usage import render_usage


def test_usage_rollup_aggregates_per_provider(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.record_usage_sample(provider="codex", raw_json="{}", input_tokens=100, output_tokens=20, total_tokens=120)
    db.record_usage_sample(provider="codex", raw_json="{}", input_tokens=50, output_tokens=10, total_tokens=60)
    db.record_usage_sample(provider="claude", raw_json="{}", input_tokens=200, output_tokens=40, total_tokens=240)
    rollup = db.usage_rollup()
    by = {r["provider"]: r for r in rollup}
    assert by["codex"]["input_tokens"] == 150
    assert by["codex"]["samples"] == 2
    assert by["claude"]["total_tokens"] == 240
    text = render_usage(rollup)
    assert "codex" in text and "claude" in text
