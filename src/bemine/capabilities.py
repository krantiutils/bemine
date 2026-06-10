from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def build_capability_manifest(
    credential_rows: Iterable[Mapping[str, Any]],
    *,
    device_id: str,
    tools: Mapping[str, Any] | None = None,
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
        "tools": dict(tools) if tools is not None else detect_tool_capabilities(),
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

    tools = manifest.get("tools") or {}
    if tools:
        lines.extend(["", "## Tools"])
        for name, tool in sorted(tools.items()):
            available = "available" if tool.get("available") else "missing"
            status = str(tool.get("status") or "")
            summary = f"- `{name}`: {available}"
            if status:
                summary += f" ({status})"
            lines.append(summary)
            path = tool.get("path")
            if path:
                lines.append(f"  Path: `{path}`")
            devices = tool.get("devices") or []
            if devices:
                for device in devices:
                    lines.append(
                        "  Device: "
                        f"`{device.get('serial')}` state={device.get('state')}"
                    )
            sdk_path = tool.get("sdk_path")
            if sdk_path:
                lines.append(f"  SDK: `{sdk_path}`")

    lines.extend(["", "## Worker Contract"])
    lines.extend(f"- {rule}" for rule in manifest["worker_contract"])
    lines.append("")
    return "\n".join(lines)


def detect_tool_capabilities() -> dict[str, Any]:
    return {
        "adb": _adb_capability(),
        "android_sdk": _android_sdk_capability(),
    }


def _access_mode(approval_policy: str) -> str:
    if approval_policy == "baseline_shared":
        return "available"
    return "approval_required"


def _adb_capability() -> dict[str, Any]:
    path = shutil.which("adb")
    if not path:
        return {"available": False, "status": "not_installed", "devices": []}
    result: dict[str, Any] = {
        "available": True,
        "path": path,
        "status": "unknown",
        "devices": [],
    }
    try:
        proc = subprocess.run(
            [path, "devices", "-l"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result
    result["devices"] = _parse_adb_devices(proc.stdout)
    if proc.returncode != 0:
        result["status"] = "error"
        result["error"] = (proc.stderr or proc.stdout).strip()
    elif result["devices"]:
        states = {device["state"] for device in result["devices"]}
        result["status"] = "authorized" if states == {"device"} else "attention_required"
    else:
        result["status"] = "no_devices"
    return result


def _android_sdk_capability() -> dict[str, Any]:
    candidates = [
        os.environ.get("ANDROID_HOME", ""),
        os.environ.get("ANDROID_SDK_ROOT", ""),
        str(Path.home() / "Android" / "Sdk"),
        str(Path.home() / "android-sdk"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return {
                "available": True,
                "sdk_path": str(path),
                "status": "available",
            }
    return {"available": False, "status": "not_found"}


def _parse_adb_devices(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        devices.append({"serial": parts[0], "state": parts[1]})
    return devices
