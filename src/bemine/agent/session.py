from __future__ import annotations

from pathlib import Path

from bemine.config import BemineSettings
from bemine.db import BemineDB


def session_key_for(*, platform: str, chat_type: str, chat_id: str,
                    thread_id: str | None) -> str:
    """Deterministic session key (mirrors Hermes build_session_key): threads/DMs are
    shared lanes keyed on stable ids, never on message content."""
    parts = ["agent", platform, chat_type, chat_id]
    if thread_id:
        parts.append(thread_id)
    return ":".join(parts)


def ensure_session(db: BemineDB, settings: BemineSettings, *, channel_id: str,
                   chat_type: str, chat_id: str, thread_id: str | None, project_id: str,
                   runtime: str, model: str, cwd: Path) -> str:
    """Resolve the session bound to this Discord surface, creating it once if absent.
    Binding key is the channel/thread id (a column on bemine_sessions)."""
    existing = db.get_bemine_session_by_channel(channel_id)
    if existing:
        return str(existing["session_id"])
    session_id = session_key_for(platform="discord", chat_type=chat_type,
                                 chat_id=chat_id, thread_id=thread_id)
    db.upsert_bemine_session(
        session_id=session_id, channel_id=channel_id, project_id=project_id,
        runtime=runtime, model=model, device_id=settings.device_id,
        cwd=str(cwd), status="active", summary="",
    )
    return session_id
