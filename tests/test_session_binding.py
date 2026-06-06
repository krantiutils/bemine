from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.agent.session import session_key_for, ensure_session


def _db(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    return settings, BemineDB(settings.db_path)


def test_session_key_is_deterministic_per_surface():
    dm = session_key_for(platform="discord", chat_type="dm", chat_id="u1", thread_id=None)
    dm2 = session_key_for(platform="discord", chat_type="dm", chat_id="u1", thread_id=None)
    thread = session_key_for(platform="discord", chat_type="thread", chat_id="parent", thread_id="t9")
    assert dm == dm2
    assert dm != thread
    assert thread.endswith("t9")


def test_ensure_session_resumes_same_row_for_same_channel(tmp_path):
    settings, db = _db(tmp_path)
    s1 = ensure_session(db, settings, channel_id="chan1", chat_type="thread",
                        chat_id="parent", thread_id="chan1", project_id="p1",
                        runtime="kimi", model="kimi-k2.6", cwd=tmp_path)
    s2 = ensure_session(db, settings, channel_id="chan1", chat_type="thread",
                        chat_id="parent", thread_id="chan1", project_id="p1",
                        runtime="kimi", model="kimi-k2.6", cwd=tmp_path)
    assert s1 == s2
