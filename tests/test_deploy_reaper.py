from bemine.config import BemineSettings
from bemine.deploy.state import DeployState, state_path
from bemine.deploy import pipeline as P
from tests.test_deploy_pipeline import _Clock, _deps  # reuse fakes


def _settings(tmp_path):
    return BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")


def test_reap_resets_stale_deploying_to_failed(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    DeployState(phase="deploying", updated_at=clock.t - (P.STALE_DEPLOY_SECS + 1),
                channel_id="c1").save(state_path(s))
    deps, calls = _deps(clock=clock)
    P.reap_stale_deploy(s, deps=deps)
    st = DeployState.load(state_path(s))
    assert st.phase == "failed"
    assert any("stuck" in t.lower() or "stale" in t.lower() for t in calls["echo"])


def test_reap_leaves_recent_deploying(tmp_path):
    s = _settings(tmp_path)
    clock = _Clock()
    DeployState(phase="deploying", updated_at=clock.t - 5).save(state_path(s))
    deps, calls = _deps(clock=clock)
    P.reap_stale_deploy(s, deps=deps)
    assert DeployState.load(state_path(s)).phase == "deploying"


def test_reap_noop_when_not_deploying(tmp_path):
    s = _settings(tmp_path)
    DeployState(phase="confirmed").save(state_path(s))
    deps, calls = _deps(clock=_Clock())
    P.reap_stale_deploy(s, deps=deps)
    assert DeployState.load(state_path(s)).phase == "confirmed"
