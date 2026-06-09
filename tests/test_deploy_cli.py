from bemine.config import BemineSettings
from bemine.cli import main
from bemine.deploy.state import DeployState, state_path


def test_cli_deploy_status(tmp_path, capsys):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    DeployState(phase="confirmed", target_sha="abcdef123").save(state_path(settings))
    main(["--state-dir", str(settings.state_dir), "deploy", "status"])
    assert "confirmed" in capsys.readouterr().out


def test_cli_deploy_runs_pipeline(tmp_path, monkeypatch, capsys):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    import bemine.cli as cli
    called = {"run": 0}

    def fake_run_deploy(s, *, channel_id="", deps=None, self_restart_daemon=True):
        called["run"] += 1
        from bemine.deploy.state import DeployState
        return DeployState(phase="confirmed", target_sha="newsha")
    monkeypatch.setattr(cli, "run_deploy", fake_run_deploy, raising=False)

    main(["--state-dir", str(settings.state_dir), "deploy"])
    assert called["run"] == 1
    assert "confirmed" in capsys.readouterr().out
