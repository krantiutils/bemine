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
