from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_default_state_is_idle():
    st = DeployState()
    assert st.phase == "idle"
    assert st.boot_attempts == 0
    assert st.target_sha == ""


def test_save_then_load_roundtrip(tmp_path):
    s = _settings(tmp_path)
    p = state_path(s)
    st = DeployState(phase="daemon_refresh", target_sha="t1", rollback_sha="r1",
                     channel_id="c1", boot_attempts=2, boot_ts=99.0)
    st.save(p)
    loaded = DeployState.load(p)
    assert loaded.phase == "daemon_refresh"
    assert loaded.target_sha == "t1"
    assert loaded.rollback_sha == "r1"
    assert loaded.channel_id == "c1"
    assert loaded.boot_attempts == 2
    assert loaded.boot_ts == 99.0


def test_load_missing_returns_idle(tmp_path):
    assert DeployState.load(state_path(_settings(tmp_path))).phase == "idle"


def test_state_path_under_state_dir(tmp_path):
    s = _settings(tmp_path)
    assert state_path(s) == s.state_dir / "deploy" / "state.json"
