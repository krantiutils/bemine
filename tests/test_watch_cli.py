from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.cli import main


def _prime(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_bemine_session(
        session_id="s1", channel_id="c1", project_id="channel:demo", runtime="kimi",
        model="kimi-k2.6", device_id="cdjk", cwd="/tmp", status="active", summary="",
    )
    return settings, db


def test_cli_watch_start_status_stop(tmp_path, capsys):
    settings, db = _prime(tmp_path)
    base = ["--state-dir", str(settings.state_dir)]
    main(base + ["watch", "start", "--project", "channel:demo",
                 "--brief", "drive it", "--interval", "120"])
    row = db.get_project_watcher("channel:demo")
    assert row["status"] == "active"
    assert row["interval_secs"] == 120
    assert row["channel_id"] == "c1"

    main(base + ["watch", "list"])
    assert "channel:demo" in capsys.readouterr().out

    main(base + ["watch", "stop", "--project", "channel:demo"])
    assert db.get_project_watcher("channel:demo")["status"] == "stopped"
