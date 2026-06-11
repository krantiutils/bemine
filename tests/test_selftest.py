from __future__ import annotations

import shutil

import pytest

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.selftest import render_selftest, run_selftest


def _settings(tmp_path):
    return BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        claude_home=tmp_path / "claude_home",
        codex_home=tmp_path / "codex_home",
        device_id="cdjk",
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_selftest_runs_continuity_loop_and_reports_checks(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)

    result = run_selftest(settings, db=db)

    names = {check["name"] for check in result["checks"]}
    assert "continuity:kill-resume" in names
    assert "recovery:classify-usage-limit" in names
    assert "recovery:parse-relative-reset" in names
    assert "recovery:approaching-not-limit" in names
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["continuity:kill-resume"]["ok"] is True
    assert by_name["recovery:classify-usage-limit"]["ok"] is True
    # no leftover worker rows or tmux sessions
    assert all(
        not str(row["name"]).startswith("bemine-selftest")
        for row in db.list_managed_sessions()
    )
    rendered = render_selftest(result)
    assert "Bemine selftest:" in rendered
    assert "continuity:kill-resume" in rendered


def test_selftest_job_failure_marks_job_failed(tmp_path, monkeypatch):
    from bemine.jobs import run_job_by_id

    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "bemine.selftest.run_selftest",
        lambda s, db=None: {
            "ok": False,
            "checks": [
                {"name": "continuity:kill-resume", "ok": False, "detail": "boom"}
            ],
            "failed": ["continuity:kill-resume"],
            "ran_at": 0.0,
        },
    )

    result = run_job_by_id(settings, "selftest")

    assert result.status == "failed"
    assert "continuity:kill-resume" in result.output
