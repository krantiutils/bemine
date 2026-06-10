from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bemine.index.status import _redact


def render_channel_summaries(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return "No local Discord channels or threads found.\n"
    lines = ["Local Discord channels/threads:"]
    for row in rows:
        channel_id = str(row.get("channel_id") or "")
        total = int(row.get("total_messages") or 0)
        inbound = int(row.get("inbound_messages") or 0)
        outbound = int(row.get("outbound_messages") or 0)
        created_at = str(row.get("latest_created_at") or "")
        author = str(row.get("latest_author_name") or "")
        content = _clip_one_line(str(row.get("latest_content") or ""), 180)
        lines.append(
            f"- `{channel_id}` messages={total} in={inbound} out={outbound} "
            f"last={created_at} {author}: {content}"
        )
    lines.append("")
    return "\n".join(lines)


def render_transcript(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return "No local Discord transcript messages found.\n"
    lines = ["Local Discord transcript:"]
    for row in rows:
        channel_id = str(row.get("channel_id") or "")
        created_at = str(row.get("created_at") or "")
        author = str(row.get("author_name") or "")
        role = str(row.get("role") or "")
        content = _clip_block(str(row.get("content") or ""), 1200)
        lines.append(f"[{created_at}] `{channel_id}` {author} ({role}):")
        lines.append(content or "(empty)")
    lines.append("")
    return "\n".join(lines)


def redact_message_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("content", "latest_content"):
            if key in item:
                item[key] = _redact(str(item[key]))
        redacted.append(item)
    return redacted


def _clip_one_line(text: str, limit: int) -> str:
    cleaned = " ".join(_redact(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def _clip_block(text: str, limit: int) -> str:
    cleaned = _redact(text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."
