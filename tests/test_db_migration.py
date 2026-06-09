import sqlite3
from bemine.config import BemineSettings
from bemine.db import BemineDB


def test_project_watchers_unchanged_sends_column_exists(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(s.db_path)
    db.upsert_project_watcher(project_id="p", channel_id="c", brief="b", interval_secs=300)
    db.update_project_watcher("p", unchanged_sends=2)
    assert db.get_project_watcher("p")["unchanged_sends"] == 2


def test_migration_adds_column_to_old_table(tmp_path):
    # Simulate a pre-existing project_watchers table WITHOUT unchanged_sends.
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    # First init creates current schema; drop+recreate an old-shaped table.
    BemineDB(s.db_path)
    with sqlite3.connect(s.db_path) as conn:
        conn.execute("DROP TABLE project_watchers")
        conn.execute(
            "CREATE TABLE project_watchers (project_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, "
            "brief TEXT NOT NULL, interval_secs INTEGER NOT NULL, status TEXT NOT NULL, "
            "last_tick_at REAL NOT NULL DEFAULT 0, awaiting_reply INTEGER NOT NULL DEFAULT 0, "
            "empty_ticks INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL, created_at REAL NOT NULL)"
        )
    # Re-init should add the missing column without error.
    db = BemineDB(s.db_path)
    cols = [r[1] for r in sqlite3.connect(s.db_path).execute("PRAGMA table_info(project_watchers)")]
    assert "unchanged_sends" in cols
    db.upsert_project_watcher(project_id="p", channel_id="c", brief="b", interval_secs=300)
    db.update_project_watcher("p", unchanged_sends=1)
    assert db.get_project_watcher("p")["unchanged_sends"] == 1
