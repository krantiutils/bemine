from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def build_capability_manifest(
    credential_rows: Iterable[Mapping[str, Any]],
    *,
    device_id: str,
) -> dict[str, Any]:
    credentials: list[dict[str, Any]] = []
    for row in credential_rows:
        credential_id = str(row["credential_id"])
        approval_policy = str(row["approval_policy"])
        access = _access_mode(approval_policy)
        item: dict[str, Any] = {
            "credential_id": credential_id,
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "access": access,
            "approval_policy": approval_policy,
            "totp_required": bool(row["totp_required"]),
        }
        if access == "approval_required":
            item["request_command"] = (
                f"bemine permissions ask {credential_id} "
                '--reason "<why this session needs it>"'
            )
            item["proxy_command"] = "bemine proxy check <approved_request_id>"
            item["proxy_mode"] = "brokered_operations_only"
        credentials.append(item)

    return {
        "schema": "bemine.capabilities.v1",
        "device_id": device_id,
        "credentials": credentials,
        "worker_contract": [
            "Use baseline_shared credentials only for normal identity operations.",
            "Never inspect credential storage paths or secret files directly.",
            "For approval_required credentials, call the request_command with a concrete reason.",
            "Wait for the permission request to be approved before using gated access.",
        ],
    }


def render_capability_manifest(manifest: Mapping[str, Any]) -> str:
    lines = [
        "# Bemine Capability Manifest",
        "",
        f"Schema: `{manifest['schema']}`",
        f"Device: `{manifest['device_id']}`",
        "",
        "## Credentials",
    ]

    for credential in manifest["credentials"]:
        suffix = " totp" if credential["totp_required"] else ""
        lines.append(
            "- "
            f"`{credential['credential_id']}` - {credential['name']} "
            f"({credential['kind']}): {credential['access']} "
            f"[{credential['approval_policy']}{suffix}]"
        )
        request_command = credential.get("request_command")
        if request_command:
            lines.append(f"  Request: `{request_command}`")
        proxy_command = credential.get("proxy_command")
        if proxy_command:
            lines.append(
                f"  Proxy: `{proxy_command}` ({credential['proxy_mode']})"
            )

    lines.extend(["", "## Worker Contract"])
    lines.extend(f"- {rule}" for rule in manifest["worker_contract"])
    lines.append("")
    return "\n".join(lines)


def _access_mode(approval_policy: str) -> str:
    if approval_policy == "baseline_shared":
        return "available"
    return "approval_required"
