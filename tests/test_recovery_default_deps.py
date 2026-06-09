from bemine.config import BemineSettings
from bemine.jobs.auto_recovery import default_recovery_deps


def test_default_recovery_deps_builds(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    deps = default_recovery_deps(s)
    assert callable(deps.get_statuses)
    assert callable(deps.decide)
    assert callable(deps.escalate)
    assert callable(deps.audit)
    assert deps.tmux is not None
