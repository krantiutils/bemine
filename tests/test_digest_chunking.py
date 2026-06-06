from mayor_discord.send import chunk_for_discord


def test_chunk_for_discord_splits_on_limit():
    text = "x" * 4500
    chunks = chunk_for_discord(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text
    assert len(chunks) == 3


def test_chunk_for_discord_short_text_single_chunk():
    chunks = chunk_for_discord("hello", limit=2000)
    assert chunks == ["hello"]


def test_chunk_for_discord_prefers_newline_boundaries():
    text = "a" * 1500 + "\n" + "b" * 1000
    chunks = chunk_for_discord(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text
