import bemine.jobs.project_watcher as pw


def test_echo_to_channel_calls_send_message(monkeypatch):
    sent = {}

    async def _fake_send(*, settings, channel_id, text, files, reply_to=None):
        sent["channel_id"] = channel_id
        sent["text"] = text
        return {"id": "1"}

    monkeypatch.setattr(pw, "send_message", _fake_send)
    monkeypatch.setattr(pw.DiscordSettings, "load", classmethod(lambda cls: object()))
    pw.echo_to_channel("c1", "hello")
    assert sent == {"channel_id": "c1", "text": "hello"}
