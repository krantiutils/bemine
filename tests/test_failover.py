from __future__ import annotations

import sqlite3
from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.jobs.failover import tick_device_failover
from bemine.workers.workers import WorkerController

from tests.test_goals import FakeTmux


def _settings(tmp_path):
    return BemineSettings.load(
        state_dir=tmp_path / ".bemine",
        vault_dir=tmp_path / "vault",
        device_id="cdjk",
    )


def _remote_worker(db, *, cwd: str, name: str = "goal-mbp", device: str = "mbp"):
    db.upsert_managed_session(
        name=name,
        runtime="claude",
        transport="ssh-tmux",
        device_id=device,
        project_id="proj_remote",
        cwd=cwd,
        command="claude --dangerously-skip-permissions",
        status="running",
    )


def test_failover_revives_remote_worker_after_threshold(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    project = tmp_path / "repo"
    project.mkdir()
    _remote_worker(db, cwd=str(project))
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    revived = []
    for _ in range(3):
        revived = tick_device_failover(
            settings,
            db=db,
            controller=controller,
            probe=lambda device: False,
            send_sleep=lambda _: None,
        )

    assert revived == [{"original": "goal-mbp", "replacement": "goal-mbp-fo"}]
    assert "goal-mbp-fo" in tmux.sessions
    started = tmux.started_commands["goal-mbp-fo"]
    assert "claude --dangerously-skip-permissions" in started
    assert "--session-id " in started  # fresh pinned conversation
    sent = "".join(tmux.sent["goal-mbp-fo"])
    assert "Bemine Failover Revival" in sent
    assert "`mbp` lost power/network" in sent
    assert "NOT available" in sent
    original = db.get_managed_session("goal-mbp")
    assert original is not None and original["status"] == "failed-over"
    with sqlite3.connect(settings.db_path) as conn:
        events = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM audit_events WHERE category='failover'"
            )
        ]
    assert "worker_failover_revived" in events


def test_failover_waits_for_threshold_and_never_revives_twice(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    project = tmp_path / "repo"
    project.mkdir()
    _remote_worker(db, cwd=str(project))
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    args = dict(
        db=db, controller=controller, probe=lambda d: False, send_sleep=lambda _: None
    )
    assert tick_device_failover(settings, **args) == []  # 1st failure
    assert tick_device_failover(settings, **args) == []  # 2nd failure
    assert len(tick_device_failover(settings, **args)) == 1  # 3rd -> revive
    # worker row is now failed-over; nothing left to revive
    assert tick_device_failover(settings, **args) == []


def test_failover_reachable_device_resets_counter(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    project = tmp_path / "repo"
    project.mkdir()
    _remote_worker(db, cwd=str(project))
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    flaky = iter([False, False, True, False, False])
    args = dict(db=db, controller=controller, send_sleep=lambda _: None)
    for expected in ([], [], [], [], []):
        assert (
            tick_device_failover(settings, probe=lambda d: next(flaky), **args)
            == expected
        )
    assert "goal-mbp-fo" not in tmux.sessions


def test_failover_skips_worker_without_local_checkout(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    _remote_worker(db, cwd="/Users/cdjk/only-on-mac/repo")
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    args = dict(
        db=db, controller=controller, probe=lambda d: False, send_sleep=lambda _: None
    )
    for _ in range(4):
        assert tick_device_failover(settings, **args) == []

    assert tmux.sessions == set()
    with sqlite3.connect(settings.db_path) as conn:
        events = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM audit_events WHERE category='failover'"
            )
        ]
    assert events.count("worker_failover_skipped") == 1


def test_failover_uses_project_devices_mapping_for_local_cwd(tmp_path):
    settings = _settings(tmp_path)
    db = BemineDB(settings.db_path)
    local_checkout = tmp_path / "local-checkout"
    local_checkout.mkdir()
    db.upsert_device("cdjk")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id, name, cwd, first_seen_at, last_seen_at)"
            " VALUES('proj_remote', 'repo', ?, 1, 1)",
            (str(local_checkout),),
        )
        conn.execute(
            "INSERT INTO project_devices(project_id, device_id, cwd, last_seen_at)"
            " VALUES('proj_remote', 'cdjk', ?, 1)",
            (str(local_checkout),),
        )
    _remote_worker(db, cwd="/Users/cdjk/only-on-mac/repo")
    tmux = FakeTmux()
    controller = WorkerController(settings, db=db, tmux=tmux)  # type: ignore[arg-type]

    args = dict(
        db=db, controller=controller, probe=lambda d: False, send_sleep=lambda _: None
    )
    revived = []
    for _ in range(3):
        revived = tick_device_failover(settings, **args)

    assert len(revived) == 1
    assert str(local_checkout) in tmux.started_commands["goal-mbp-fo"]
