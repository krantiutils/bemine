from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .db import BemineDB


Sender = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class PermissionNotificationResult:
    request_id: str
    message_id: str
    channel_id: str
    text: str


def format_permission_request_message(request: Mapping[str, Any]) -> str:
    lines = [
        "**Bemine permission request**",
        f"Request: `{request['request_id']}`",
        f"Credential: `{request['credential_id']}` - {request['credential_name']}",
        f"Reason: {request['reason']}",
    ]

    context = []
    if request["runtime"]:
        context.append(f"runtime `{request['runtime']}`")
    if request["project_id"]:
        context.append(f"project `{request['project_id']}`")
    if request["session_id"]:
        context.append(f"session `{request['session_id']}`")
    if request["device_id"]:
        context.append(f"device `{request['device_id']}`")
    if context:
        lines.append("Context: " + ", ".join(context))

    lines.extend(
        [
            "",
            f"Approve: `bemine approve {request['request_id']} <totp>`",
            f"Deny: `bemine deny {request['request_id']}`",
        ]
    )
    return "\n".join(lines)


def notify_permission_request(
    db: BemineDB,
    request_id: str,
    *,
    sender: Sender,
) -> PermissionNotificationResult:
    request = db.get_permission_request(request_id)
    if request is None:
        raise ValueError(f"unknown permission request: {request_id}")

    text = format_permission_request_message(request)
    sent = dict(sender(text))
    message_id = str(sent.get("id") or sent.get("message_id") or "")
    channel_id = str(sent.get("channel_id") or "")

    if message_id:
        db.set_permission_discord_message(
            request_id=request_id,
            discord_message_id=message_id,
        )

    db.record_audit_event(
        category="permission",
        event_type="permission_notification_sent",
        actor="permission_notifier",
        device_id=request["device_id"],
        project_id=request["project_id"],
        session_id=request["session_id"],
        payload_json=json.dumps(
            {
                "request_id": request_id,
                "credential_id": request["credential_id"],
                "channel_id": channel_id,
                "discord_message_id": message_id,
            },
            sort_keys=True,
        ),
    )
    return PermissionNotificationResult(
        request_id=request_id,
        message_id=message_id,
        channel_id=channel_id,
        text=text,
    )


def notify_permission_request_via_discord(
    db: BemineDB,
    request_id: str,
) -> PermissionNotificationResult:
    from bemine.discord.config import Settings
    from bemine.discord.send import send_message
    from bemine.discord.store import Store

    base = Settings.load()
    settings = Settings(
        token=base.token,
        home_channel_id=base.home_channel_id,
        allowed_users=base.allowed_users,
        allow_all_users=base.allow_all_users,
        mayor_home=db.path.parent,
        db_path=db.path,
        inbox_dir=db.path.parent / "inbox",
    )
    settings.require_token()
    settings.require_home_channel()

    def sender(text: str) -> Mapping[str, Any]:
        result = asyncio.run(
            send_message(
                settings=settings,
                channel_id=settings.home_channel_id,
                text=text,
                files=[],
            )
        )
        Store(db.path).record_outbound(
            discord_message_id=str(result.get("id") or ""),
            channel_id=settings.home_channel_id,
            content=text,
            files=[],
        )
        return result

    return notify_permission_request(db, request_id, sender=sender)
