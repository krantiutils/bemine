from __future__ import annotations

from bemine.capabilities import (
    _parse_adb_devices,
    build_capability_manifest,
    render_capability_manifest,
)


def test_parse_adb_devices_reports_authorization_state() -> None:
    devices = _parse_adb_devices(
        "\n".join(
            [
                "List of devices attached",
                "RZCTC0M17SJ unauthorized usb:1-3 transport_id:1",
                "emulator-5554 device product:sdk",
            ]
        )
    )

    assert devices == [
        {"serial": "RZCTC0M17SJ", "state": "unauthorized"},
        {"serial": "emulator-5554", "state": "device"},
    ]


def test_capability_manifest_renders_android_tools() -> None:
    manifest = build_capability_manifest(
        [],
        device_id="cdjk",
        tools={
            "adb": {
                "available": True,
                "path": "/usr/bin/adb",
                "status": "attention_required",
                "devices": [{"serial": "RZCTC0M17SJ", "state": "unauthorized"}],
            },
            "android_sdk": {
                "available": True,
                "sdk_path": "/home/cdjk/Android/Sdk",
                "status": "available",
            },
        },
    )

    text = render_capability_manifest(manifest)

    assert manifest["tools"]["adb"]["devices"][0]["state"] == "unauthorized"
    assert "## Tools" in text
    assert "`adb`: available (attention_required)" in text
    assert "state=unauthorized" in text
    assert "`android_sdk`: available" in text
