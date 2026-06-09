from bemine.config import BemineSettings
from bemine.deploy import pipeline as P


def test_default_deps_builds_callable_bundle(tmp_path):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    deps = P.default_deps(s)
    assert callable(deps.run_tests)
    assert callable(deps.restart)
    assert callable(deps.git_reset_hard)
    assert callable(deps.read_hb)
    assert callable(deps.echo)
    assert callable(deps.head_sha)
    assert callable(deps.is_clean)
    assert isinstance(deps.now(), float)
