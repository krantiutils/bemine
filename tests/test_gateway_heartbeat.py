from bemine.config import BemineSettings
from bemine.deploy.heartbeat import read_heartbeat
from bemine.discord.gateway import publish_gateway_heartbeat


def test_publish_gateway_heartbeat_writes_file(tmp_path, monkeypatch):
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    import bemine.discord.gateway as gw
    monkeypatch.setattr(gw, "current_head_sha", lambda *a, **k: "gwsha")
    publish_gateway_heartbeat(s)
    hb = read_heartbeat(s, "gateway")
    assert hb["sha"] == "gwsha"
    assert hb["service"] == "gateway"


def test_publish_falls_back_when_settings_has_no_state_dir(tmp_path, monkeypatch):
    # Regression: the gateway's real self.settings is the Discord Settings dataclass, which
    # has NO state_dir. publish_gateway_heartbeat must fall back to BemineSettings.load()
    # and still write the heartbeat (to the canonical state dir the deploy pipeline reads).
    s = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    import bemine.discord.gateway as gw
    monkeypatch.setattr(gw, "current_head_sha", lambda *a, **k: "gwsha2")
    monkeypatch.setattr(gw.BemineSettings, "load", classmethod(lambda cls, *a, **k: s))

    class _DiscordSettingsLike:  # mirrors bemine.discord.config.Settings: no state_dir
        db_path = tmp_path / "bemine.db"
        inbox_dir = tmp_path / "inbox"

    publish_gateway_heartbeat(_DiscordSettingsLike())
    assert read_heartbeat(s, "gateway")["sha"] == "gwsha2"
