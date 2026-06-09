from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path
from bemine.deploy import pipeline as P
from tests.test_deploy_pipeline import _Clock, _deps  # reuse fakes


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_boot_guard_noop_when_not_refreshing(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="idle").save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.boot_guard(s, deps=deps)
    assert calls["reset"] == []
    assert DeployState.load(state_path(s)).phase == "idle"


def test_boot_guard_increments_and_proceeds_under_limit(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="daemon_refresh", rollback_sha="old", boot_attempts=0).save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.boot_guard(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.boot_attempts == 1
    assert st.phase == "daemon_refresh"
    assert calls["reset"] == []


def test_boot_guard_rolls_back_over_limit(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="daemon_refresh", rollback_sha="old", boot_attempts=2,
                channel_id="c1").save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.boot_guard(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.boot_attempts == 3
    assert st.phase == "rolled_back"
    assert calls["reset"] == ["old"]
    assert "gateway" in calls["restart"] and "daemon" in calls["restart"]


def test_maybe_confirm_after_window(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    DeployState(phase="daemon_refresh", target_sha="newsha", boot_ts=clock.t - 61,
                channel_id="c1").save(state_path(s))
    deps, calls = _deps(clock=clock)
    P.maybe_confirm_deploy(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.phase == "confirmed"
    assert st.boot_attempts == 0
    assert any("complete" in t.lower() for t in calls["echo"])


def test_maybe_confirm_waits_within_window(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    DeployState(phase="daemon_refresh", target_sha="newsha", boot_ts=clock.t - 5).save(state_path(s))
    deps, calls = _deps(clock=clock)
    P.maybe_confirm_deploy(s, deps=deps)
    assert DeployState.load(state_path(s)).phase == "daemon_refresh"
