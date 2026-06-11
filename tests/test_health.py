from __future__ import annotations

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.deploy.heartbeat import write_heartbeat
from bemine.deploy.state import DeployState, state_path
from bemine.health import collect_health, render_continuity, render_health
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


def test_health_reports_continuity_restartable_missing_worker(tmp_path, monkeypatch):
    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        device_id="cdjk",
    )
    project = tmp_path / "repo"
    project.mkdir()
    DeployState(phase="confirmed", target_sha="sha1").save(state_path(settings))
    write_heartbeat(settings, "daemon", "sha1")
    write_heartbeat(settings, "gateway", "sha1")
    db = BemineDB(settings.db_path)
    db.upsert_managed_session(
        name="goal-restart",
        runtime="shell",
        transport="tmux",
        device_id="cdjk",
        project_id="proj_restart",
        cwd=str(project),
        command="cat",
        status="running",
    )

    class FakeTmux:
        def exists(self, _name):
            return False

    monkeypatch.setattr("bemine.health.TmuxAdapter", lambda: FakeTmux())

    payload = collect_health(
        settings,
        db=db,
        unit_statuses=[
            UnitStatus("bemine-daemon.service", "loaded", "active", "running", "enabled", 111),
            UnitStatus("mayor-discord-gateway.service", "loaded", "active", "running", "enabled", 222),
        ],
    )

    assert payload["ok"] is False
    assert payload["continuity"]["applies_to_device"] is True
    assert payload["continuity"]["mode"] == "startup/poll recovery, not a shutdown hook"
    assert "Bemine-managed local tmux workers" in payload["continuity"]["covers"]
    assert "adopted/manual tmux sessions" in payload["continuity"]["excludes"]
    assert payload["continuity"]["restartable_missing"] == 1
    assert payload["continuity"]["missing_workers"][0]["name"] == "goal-restart"
    assert any("continuity has 1 restartable missing worker" in issue for issue in payload["issues"])
    assert "Continuity:" in render_health(payload)
    rendered = render_continuity(payload)
    assert "Mode: startup/poll recovery, not a shutdown hook" in rendered
    assert "Covers: Bemine-managed local tmux workers with restartable commands" in rendered
    assert "Excludes: adopted/manual tmux sessions, remote device sessions, arbitrary terminals" in rendered


def test_health_summarizes_recent_recovery_events(tmp_path):
    settings = _settings(tmp_path)
    DeployState(phase="confirmed", target_sha="sha1").save(state_path(settings))
    write_heartbeat(settings, "daemon", "sha1")
    write_heartbeat(settings, "gateway", "sha1")
    db = BemineDB(settings.db_path)
    db.record_audit_event(
        category="recovery",
        event_type="llm_escalate",
        actor="auto_recovery",
        device_id="mbp",
        payload_json="{}",
    )
    db.record_audit_event(
        category="recovery",
        event_type="limit_wait",
        actor="auto_recovery",
        device_id="mbp2",
        payload_json="{}",
    )
    db.record_audit_event(
        category="recovery",
        event_type="recovered",
        actor="auto_recovery",
        device_id="cdjk",
        payload_json="{}",
    )

    payload = collect_health(
        settings,
        db=db,
        unit_statuses=[
            UnitStatus("bemine-daemon.service", "loaded", "active", "running", "enabled", 111),
            UnitStatus("mayor-discord-gateway.service", "loaded", "active", "running", "enabled", 222),
        ],
    )

    assert payload["recovery"]["recent"] == 3
    assert payload["recovery"]["escalations"] == 1
    assert payload["recovery"]["limit_waits"] == 1
    assert payload["recovery"]["recovered"] == 1
    assert "Recovery: recent=3 escalations=1 limit_waits=1 recovered=1" in render_health(payload)
