from bemine.config import BemineSettings
from bemine.db import BemineDB
from bemine.discord.gateway import (
    maybe_add_backlog_from_discord_message,
    maybe_consume_watcher_reply,
    maybe_start_goal_from_discord_message,
    parse_backlog_command,
    parse_goal_command,
)


class FakeChannel:
    id = "channel-rai"
    name = "rai-speech-toolkit"
    category = None


class FakeMessage:
    channel = FakeChannel()
    channel_mentions = []
    content = 'goal rai-speech-toolkit: "train the first checkpoint"'
    author = type("Author", (), {"id": "user-1"})()


def test_consume_when_awaiting(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)
    db.update_project_watcher("channel:p", awaiting_reply=1)

    import bemine.jobs.project_watcher as pw
    monkeypatch.setattr(pw, "relay_user_reply", lambda s, d, w, text, deps: True)

    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="use staging")
    assert handled is True


def test_not_consumed_when_not_awaiting(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)  # awaiting_reply defaults to 0
    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="hi")
    assert handled is False


def test_not_consumed_when_no_watcher(tmp_path):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    handled = maybe_consume_watcher_reply(settings, db, channel_id="cX", text="hi")
    assert handled is False


def test_relay_failure_pauses_and_does_not_consume(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    db.upsert_project_watcher(project_id="channel:p", channel_id="c1", brief="b",
                              interval_secs=300)
    db.update_project_watcher("channel:p", awaiting_reply=1)

    import bemine.jobs.project_watcher as pw
    monkeypatch.setattr(pw, "relay_user_reply", lambda s, d, w, text, deps: False)

    handled = maybe_consume_watcher_reply(settings, db, channel_id="c1", text="hi")
    assert handled is False  # not consumed -> falls through to normal handling
    row = db.get_project_watcher("channel:p")
    assert row["awaiting_reply"] == 0
    assert row["status"] == "paused"


def test_parse_goal_command_accepts_project_colon_and_quotes():
    parsed = parse_goal_command(
        'goal rai-speech-toolkit: "train the first Rai TTS checkpoint"'
    )

    assert parsed is not None
    assert parsed.project == "rai-speech-toolkit"
    assert parsed.brief == "train the first Rai TTS checkpoint"


def test_parse_goal_command_accepts_start_goal_prefix():
    parsed = parse_goal_command(
        'start a goal rai-speech-toolkit: "train the first Rai TTS checkpoint"'
    )

    assert parsed is not None
    assert parsed.project == "rai-speech-toolkit"
    assert parsed.brief == "train the first Rai TTS checkpoint"


def test_parse_backlog_command_accepts_smm_idea():
    parsed = parse_backlog_command("SMM idea: post the Rai checkpoint chart")

    assert parsed is not None
    assert parsed.owner == "SMM"
    assert parsed.kind == "idea"
    assert parsed.text == "post the Rai checkpoint chart"


def test_parse_backlog_command_accepts_remember_for_owner_without_kind():
    parsed = parse_backlog_command("remember this for SMM: post the Rai checkpoint chart")

    assert parsed is not None
    assert parsed.owner == "SMM"
    assert parsed.kind == "idea"
    assert parsed.text == "post the Rai checkpoint chart"


def test_discord_backlog_command_writes_item(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    calls = {}

    def fake_add_backlog_item(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("bemine.discord.gateway.add_backlog_item", fake_add_backlog_item)

    result = maybe_add_backlog_from_discord_message(
        settings,
        db,
        FakeMessage(),  # type: ignore[arg-type]
        "store idea for PA: review stale asks before noon",
    )

    assert result == {"ok": True}
    assert calls["settings"] == settings
    assert calls["db"] == db
    assert calls["owner"] == "PA"
    assert calls["text"] == "idea: review stale asks before noon"
    assert calls["source"] == "discord:user-1"


def test_discord_backlog_command_accepts_remember_for_owner_without_kind(
    tmp_path,
    monkeypatch,
):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    calls = {}

    def fake_add_backlog_item(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("bemine.discord.gateway.add_backlog_item", fake_add_backlog_item)

    result = maybe_add_backlog_from_discord_message(
        settings,
        db,
        FakeMessage(),  # type: ignore[arg-type]
        "remember this for SMM: post the Rai checkpoint chart",
    )

    assert result == {"ok": True}
    assert calls["settings"] == settings
    assert calls["db"] == db
    assert calls["owner"] == "SMM"
    assert calls["text"] == "idea: post the Rai checkpoint chart"
    assert calls["source"] == "discord:user-1"


def test_discord_goal_command_uses_goal_service_and_channel_context(
    tmp_path,
    monkeypatch,
):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    calls = {}

    def fake_start_goal(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return {"started": True}

    monkeypatch.setattr("bemine.discord.gateway.start_goal", fake_start_goal)

    result = maybe_start_goal_from_discord_message(
        settings,
        db,
        FakeMessage(),  # type: ignore[arg-type]
        FakeMessage.content,
        conversation_context="- voidash: gather details by reading conversation",
    )

    assert result == {"started": True}
    assert calls["settings"] == settings
    assert calls["db"] == db
    assert calls["project"] == "rai-speech-toolkit"
    assert calls["channel_id"] == "channel-rai"
    assert "train the first checkpoint" in calls["brief"]
    assert "gather details by reading conversation" in calls["brief"]


def test_discord_goal_command_accepts_start_prefix(tmp_path, monkeypatch):
    settings = BemineSettings.load(state_dir=tmp_path / ".bemine", vault_dir=tmp_path / "v")
    db = BemineDB(settings.db_path)
    calls = {}

    def fake_start_goal(settings_arg, **kwargs):
        calls["settings"] = settings_arg
        calls.update(kwargs)
        return {"started": True}

    monkeypatch.setattr("bemine.discord.gateway.start_goal", fake_start_goal)

    result = maybe_start_goal_from_discord_message(
        settings,
        db,
        FakeMessage(),  # type: ignore[arg-type]
        'start a goal rai-speech-toolkit: "train the first checkpoint"',
    )

    assert result == {"started": True}
    assert calls["settings"] == settings
    assert calls["db"] == db
    assert calls["project"] == "rai-speech-toolkit"
    assert calls["channel_id"] == "channel-rai"
    assert "train the first checkpoint" in calls["brief"]
