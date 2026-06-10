from __future__ import annotations

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.deploy.heartbeat import write_heartbeat
from bemine.deploy.state import DeployState, state_path
from bemine.health import collect_health, render_health
from bemine.jobs.systemd import UnitStatus


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_health_ok_when_services_heartbeats_and_jobs_are_clean(tmp_path):
    settings = _settings(tmp_path)
    DeployState(phase="confirmed", target_sha="sha1").save(state_path(settings))
    write_heartbeat(settings, "daemon", "sha1")
    write_heartbeat(settings, "gateway", "sha1")
    db = BemineDB(settings.db_path)

    payload = collect_health(
        settings,
        db=db,
        unit_statuses=[
            UnitStatus("bemine-daemon.service", "loaded", "active", "running", "enabled", 111),
            UnitStatus("mayor-discord-gateway.service", "loaded", "active", "running", "enabled", 222),
        ],
        now=123.0,
    )

    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["jobs"]["total"] >= 4
    assert payload["watchers"]["active"] == 0
    rendered = render_health(payload)
    assert "Bemine health: ok" in rendered
    assert "heartbeat=sha1" in rendered


def test_health_reports_service_and_job_issues(tmp_path):
    settings = _settings(tmp_path)
    DeployState(phase="deploying", target_sha="newsha").save(state_path(settings))
    write_heartbeat(settings, "daemon", "oldsha")
    db = BemineDB(settings.db_path)
    db.upsert_job(
        job_id="bad_job",
        name="Bad job",
        enabled=True,
        trigger_type="cron",
        schedule="* * * * *",
        config_json="{}",
        next_run_at="",
    )
    db.update_job_after_run(
        job_id="bad_job",
        last_run_at="2026-06-10T00:00:00+05:45",
        next_run_at="",
        success=False,
    )

    payload = collect_health(
        settings,
        db=db,
        unit_statuses=[
            UnitStatus("bemine-daemon.service", "loaded", "active", "running", "enabled", 111),
            UnitStatus("mayor-discord-gateway.service", "loaded", "failed", "failed", "enabled", 0),
        ],
    )

    assert payload["ok"] is False
    assert payload["status"] == "attention"
    assert any("deploy phase is deploying" in issue for issue in payload["issues"])
    assert any("mayor-discord-gateway" in issue for issue in payload["issues"])
    assert any("bad_job" in issue for issue in payload["issues"])
