from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.jobs import next_run_after, run_job_by_id, seed_default_jobs, upsert_reminder_job
from bemine.jobs.systemd import render_user_units, user_unit_status


def test_cron_next_run_for_daily_digest_uses_local_time() -> None:
    tz = ZoneInfo("Asia/Kathmandu")

    assert next_run_after("0 18 * * *", datetime(2026, 6, 4, 17, 59, tzinfo=tz)) == datetime(
        2026, 6, 4, 18, 0, tzinfo=tz
    )
    assert next_run_after("0 18 * * *", datetime(2026, 6, 4, 18, 0, tzinfo=tz)) == datetime(
        2026, 6, 5, 18, 0, tzinfo=tz
    )


def test_daily_digest_job_records_run_and_uses_sender(tmp_path: Path) -> None:
    codex_file = tmp_path / ".codex" / "sessions" / "2026" / "06" / "04" / (
        "rollout-2026-06-04T10-19-22-session-1.jsonl"
    )
    codex_file.parent.mkdir(parents=True)
    codex_file.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "timestamp": "2026-06-04T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "session-1", "cwd": "/repo/bemine", "model": "openai"},
                },
                {
                    "timestamp": "2026-06-04T10:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"text": "Please approve the AWS SSO code 123456."}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    seed_default_jobs(db)
    db.upsert_job(
        job_id="daily_digest",
        name="Daily project digest",
        enabled=True,
        trigger_type="cron",
        schedule="0 18 * * *",
        config_json=json.dumps(
            {
                "type": "daily_digest",
                "devices": ["local"],
                "status_limit": 4,
                "rundown_limit": 4,
                "notify_discord": True,
            },
            sort_keys=True,
        ),
        next_run_at="2026-06-04T18:00:00+05:45",
    )
    sent: list[str] = []

    result = run_job_by_id(
        settings,
        "daily_digest",
        sender=lambda text: sent.append(text) or {"id": "discord-1", "channel_id": "home"},
    )

    assert result.status == "success"
    assert result.notified is True
    assert sent and "Bemine daily digest" in sent[0]
    assert "ask: yes" in sent[0]
    assert "123456" not in sent[0]

    conn = sqlite3.connect(settings.db_path)
    try:
        run = conn.execute("SELECT status, output FROM job_runs").fetchone()
        assert run[0] == "success"
        assert "Bemine daily digest" in run[1]
        assert "123456" not in run[1]
        job = conn.execute(
            "SELECT last_run_at, next_run_at, failure_count FROM jobs WHERE job_id = ?",
            ("daily_digest",),
        ).fetchone()
        assert job[0]
        assert job[1]
        assert job[2] == 0
        events = {
            row[0]
            for row in conn.execute("SELECT event_type FROM audit_events").fetchall()
        }
        assert "job_run_finished" in events
        assert "job_notification_sent" in events
    finally:
        conn.close()


def test_reminder_job_records_run_and_sends_notification(tmp_path: Path) -> None:
    settings = BemineSettings.load(
        state_dir=tmp_path / "state",
        vault_dir=tmp_path / "vault",
        codex_home=tmp_path / ".codex",
        claude_home=tmp_path / ".claude",
        kimi_home=tmp_path / ".kimi",
        hermes_home=tmp_path / ".hermes",
        device_id="testbox",
    )
    db = BemineDB(settings.db_path)
    upsert_reminder_job(
        db,
        job_id="morning_plan",
        schedule="30 8 * * *",
        text="Review calendar, inbox, and top project blockers.",
        notify_discord=True,
        now=datetime(2026, 6, 4, 8, 0),
    )
    sent: list[str] = []

    result = run_job_by_id(
        settings,
        "morning_plan",
        sender=lambda text: sent.append(text) or {"id": "discord-2", "channel_id": "home"},
    )

    assert result.status == "success"
    assert result.notified is True
    assert sent
    assert "Bemine reminder" in sent[0]
    assert "Review calendar, inbox, and top project blockers." in sent[0]

    conn = sqlite3.connect(settings.db_path)
    try:
        run = conn.execute("SELECT status, output FROM job_runs").fetchone()
        assert run[0] == "success"
        assert "morning_plan" in run[1]
        job = conn.execute("SELECT schedule, failure_count FROM jobs WHERE job_id = 'morning_plan'").fetchone()
        assert job == ("30 8 * * *", 0)
    finally:
        conn.close()


def test_systemd_units_run_gateway_and_daemon_from_repo(tmp_path: Path) -> None:
    units = render_user_units(
        repo_dir=tmp_path / "repo",
        state_dir=tmp_path / "repo" / ".bemine",
        uv_path="/usr/bin/uv",
    )
    rendered = {unit.name: unit.text for unit in units}

    assert set(rendered) == {"bemine-daemon.service", "mayor-discord-gateway.service"}
    assert "WorkingDirectory=" + str(tmp_path / "repo") in rendered["bemine-daemon.service"]
    assert "ExecStart=/usr/bin/uv run bemine-daemon" in rendered["bemine-daemon.service"]
    assert "ExecStart=/usr/bin/uv run mayor-discord-gateway" in rendered["mayor-discord-gateway.service"]
    assert "Environment=BEMINE_HOME=" + str(tmp_path / "repo" / ".bemine") in rendered["bemine-daemon.service"]


def test_systemd_status_parses_systemctl_show() -> None:
    import subprocess

    def runner(cmd, **kwargs):
        unit = cmd[3]
        if unit == "bemine-daemon.service":
            stdout = (
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "UnitFileState=enabled\n"
                "MainPID=123\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="not-found")

    statuses = user_unit_status(
        ("bemine-daemon.service", "mayor-discord-gateway.service"),
        runner=runner,
    )

    assert statuses[0].active_state == "active"
    assert statuses[0].main_pid == 123
    assert statuses[1].active_state == "unknown"
    assert statuses[1].error == "not-found"


def test_run_once_ticks_watchers(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.jobs.project_watcher as pw

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    called = {"n": 0}
    monkeypatch.setattr(pw, "tick_all_watchers",
                        lambda s, db: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)
    BemineDaemon(settings).run_once()
    assert called["n"] == 1


def test_daemon_main_calls_boot_guard_before_run(tmp_path, monkeypatch):
    import bemine.jobs.daemon as d
    import bemine.deploy.pipeline as P
    from bemine.config import BemineSettings

    base = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    monkeypatch.setattr(BemineSettings, "load", classmethod(lambda cls, *a, **k: base))
    order = []
    monkeypatch.setattr(P, "boot_guard", lambda s: order.append("boot_guard"))

    class _FakeDaemon:
        def __init__(self, *a, **k): pass
        def run_forever(self): order.append("run_forever")
    monkeypatch.setattr(d, "BemineDaemon", _FakeDaemon)
    d.main([])
    assert order == ["boot_guard", "run_forever"]


def test_run_once_publishes_daemon_heartbeat_and_confirms(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.deploy.heartbeat as hb
    import bemine.deploy.pipeline as P
    from bemine.deploy.heartbeat import read_heartbeat

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    monkeypatch.setattr(hb, "current_head_sha", lambda *a, **k: "dsha")
    called = {"confirm": 0}
    monkeypatch.setattr(P, "maybe_confirm_deploy", lambda s: called.__setitem__("confirm", 1))
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)
    BemineDaemon(settings).run_once()
    assert read_heartbeat(settings, "daemon")["sha"] == "dsha"
    assert called["confirm"] == 1


def test_run_once_ticks_recovery(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    called = {"n": 0}
    monkeypatch.setattr(ar, "recovery_enabled", lambda: True)
    monkeypatch.setattr(ar, "tick_recovery",
                        lambda s, deps, *, now: called.__setitem__("n", called["n"] + 1))
    BemineDaemon(settings).run_once()
    assert called["n"] == 1


def test_daemon_resumes_missing_workers_on_cdjk(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.workers.workers as workers

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        device_id="cdjk",
    )
    called = {"n": 0}
    monkeypatch.setattr(
        workers,
        "resume_missing_local_workers",
        lambda s: called.__setitem__("n", called["n"] + 1),
    )
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)

    BemineDaemon(settings).run_once()

    assert called["n"] == 1


def test_daemon_audits_continuity_resumed_workers(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.workers.workers as workers

    class ResumedRecord:
        name = "goal-restart"

        def to_dict(self):
            return {
                "name": self.name,
                "runtime": "shell",
                "transport": "tmux",
                "device_id": "cdjk",
                "project_id": "proj_restart",
                "cwd": "/tmp/repo",
                "command": "cat",
                "status": "running",
                "exists": True,
            }

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        device_id="cdjk",
    )
    monkeypatch.setattr(workers, "resume_missing_local_workers", lambda s: [ResumedRecord()])
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)

    BemineDaemon(settings).run_once()

    rows = BemineDB(settings.db_path).list_audit_events(category="continuity", limit=1)
    assert rows[0]["event_type"] == "continuity_workers_resumed"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["count"] == 1
    assert payload["workers"][0]["name"] == "goal-restart"


def test_daemon_audits_continuity_errors_without_raising(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.workers.workers as workers

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        device_id="cdjk",
    )

    def fail(settings):
        raise RuntimeError("tmux unavailable")

    monkeypatch.setattr(workers, "resume_missing_local_workers", fail)
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)

    BemineDaemon(settings).run_once()

    rows = BemineDB(settings.db_path).list_audit_events(category="continuity", limit=1)
    assert rows[0]["event_type"] == "continuity_resume_failed"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["error"] == "tmux unavailable"


def test_daemon_continuity_is_cdjk_only_by_default(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.workers.workers as workers

    settings = BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "v",
        device_id="testbox",
    )
    called = {"n": 0}
    monkeypatch.setattr(
        workers,
        "resume_missing_local_workers",
        lambda s: called.__setitem__("n", called["n"] + 1),
    )
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)

    BemineDaemon(settings).run_once()

    assert called["n"] == 0


def test_daemon_heartbeat_is_frozen_at_boot(tmp_path, monkeypatch):
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar
    import bemine.deploy.heartbeat as hb
    from bemine.deploy.heartbeat import read_heartbeat

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    monkeypatch.setattr(hb, "current_head_sha", lambda *a, **k: "boot-sha")
    daemon = BemineDaemon(settings)            # captures boot sha now
    monkeypatch.setattr(hb, "current_head_sha", lambda *a, **k: "later-sha")
    monkeypatch.setattr(ar, "recovery_enabled", lambda: False)
    daemon.run_once()
    assert read_heartbeat(settings, "daemon")["sha"] == "boot-sha"   # frozen, not 'later-sha'


def test_recovery_tick_respects_poll_interval(tmp_path, monkeypatch):
    # [recovery].poll_interval_secs gates how often the daemon runs auto-recovery —
    # the daemon polls every ~30s but recovery must only act once per interval.
    from bemine.config import BemineSettings
    from bemine.jobs.core import BemineDaemon
    import bemine.jobs.auto_recovery as ar

    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    called = {"n": 0}
    monkeypatch.setattr(ar, "recovery_enabled", lambda: True)
    monkeypatch.setattr(ar, "tick_recovery",
                        lambda s, deps, *, now: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(ar, "_cfg_poll_interval", lambda: 300)
    daemon = BemineDaemon(settings)
    daemon.run_once()   # first tick fires
    daemon.run_once()   # within the interval -> skipped
    daemon.run_once()
    assert called["n"] == 1
